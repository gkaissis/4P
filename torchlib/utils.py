import multiprocessing as mp
from os.path import isfile
from random import random
from time import sleep
from typing import List, Optional, Tuple, Union

import numpy as np
import pandas as pd
import torch
import tqdm
from sklearn import metrics as mt
from syft.frameworks.torch.fl.utils import add_model, scale_model
from tabulate import tabulate


class LearningRateScheduler:
    """
    Available schedule plans:
    log_linear : Linear interpolation with log learning rate scale
    log_cosine : Cosine interpolation with log learning rate scale
    """

    def __init__(
        self,
        total_epochs: int,
        log_start_lr: float,
        log_end_lr: float,
        schedule_plan: str = "log_linear",
        restarts: Optional[int] = None,
    ):
        if restarts == 0:
            restarts = None
        self.total_epochs = (
            total_epochs if not restarts else total_epochs / (restarts + 1)
        )
        if schedule_plan == "log_linear":
            self.calc_lr = lambda epoch: np.power(
                10,
                ((log_end_lr - log_start_lr) / self.total_epochs) * epoch
                + log_start_lr,
            )
        elif schedule_plan == "log_cosine":
            self.calc_lr = lambda epoch: np.power(
                10,
                (np.cos(np.pi * (epoch / self.total_epochs)) / 2.0 + 0.5)
                * abs(log_start_lr - log_end_lr)
                + log_end_lr,
            )
        else:
            raise NotImplementedError(
                "Requested learning rate schedule {} not implemented".format(
                    schedule_plan
                )
            )

    def get_lr(self, epoch: int):
        epoch = epoch % self.total_epochs
        if (type(epoch) is int and epoch > self.total_epochs) or (
            type(epoch) is np.ndarray and np.max(epoch) > self.total_epochs
        ):
            raise AssertionError("Requested epoch out of precalculated schedule")
        return self.calc_lr(epoch)

    def adjust_learning_rate(self, optimizer: torch.optim.Optimizer, epoch: int):
        new_lr = self.get_lr(epoch)
        for param_group in optimizer.param_groups:
            param_group["lr"] = new_lr
        return new_lr


class Arguments:
    def __init__(self, cmd_args, config, mode: str = "train", verbose: bool = True):
        assert mode in ["train", "inference"], "no other mode known"
        self.batch_size = config.getint("config", "batch_size", fallback=1)
        self.test_batch_size = config.getint("config", "test_batch_size", fallback=1)
        self.train_resolution = config.getint(
            "config", "train_resolution", fallback=224
        )
        self.inference_resolution = config.getint(
            "config", "inference_resolution", fallback=self.train_resolution
        )
        self.validation_split = config.getint("config", "validation_split", fallback=10)
        self.epochs = config.getint("config", "epochs", fallback=1)
        self.lr = config.getfloat("config", "lr", fallback=1e-3)
        self.end_lr = config.getfloat("config", "end_lr", fallback=self.lr)
        self.restarts = config.getint("config", "restarts", fallback=None)
        self.momentum = config.getfloat("config", "momentum", fallback=0.5)
        self.seed = config.getint("config", "seed", fallback=1)
        self.test_interval = config.getint("config", "test_interval", fallback=1)
        self.log_interval = config.getint("config", "log_interval", fallback=10)
        # self.save_interval = config.getint("config", "save_interval", fallback=10)
        # self.save_model = config.getboolean("config", "save_model", fallback=False)
        self.optimizer = config.get("config", "optimizer", fallback="SGD")
        assert self.optimizer in ["SGD", "Adam"], "Unknown optimizer"
        if self.optimizer == "Adam":
            self.beta1 = config.getfloat("config", "beta1", fallback=0.9)
            self.beta2 = config.getfloat("config", "beta2", fallback=0.999)
        self.model = config.get("config", "model", fallback="simpleconv")
        assert self.model in ["simpleconv", "resnet-18", "vgg16"]
        self.pooling_type = config.get("config", "pooling_type", fallback="avg")
        self.pretrained = config.getboolean("config", "pretrained", fallback=False)
        self.weight_decay = config.getfloat("config", "weight_decay", fallback=0.0)
        self.weight_classes = config.getboolean(
            "config", "weight_classes", fallback=False
        )
        self.vertical_flip_prob = config.getfloat(
            "augmentation", "vertical_flip_prob", fallback=0.0
        )
        self.rotation = config.getfloat("augmentation", "rotation", fallback=0.0)
        self.translate = config.getfloat("augmentation", "translate", fallback=0.0)
        self.scale = config.getfloat("augmentation", "scale", fallback=0.0)
        self.shear = config.getfloat("augmentation", "shear", fallback=0.0)
        self.noise_std = config.getfloat("augmentation", "noise_std", fallback=1.0)
        self.noise_prob = config.getfloat("augmentation", "noise_prob", fallback=0.0)
        self.mixup = config.getboolean("augmentation", "mixup", fallback=False)
        self.mixup_prob = config.getfloat("augmentation", "mixup_prob", fallback=None)
        self.mixup_lambda = config.getfloat(
            "augmentation", "mixup_lambda", fallback=None
        )
        if self.mixup and self.mixup_prob == 1.0:
            self.batch_size *= 2
            print("Doubled batch size because of mixup")
        self.train_federated = cmd_args.train_federated if mode == "train" else False
        if self.train_federated:
            self.sync_every_n_batch = config.getint(
                "federated", "sync_every_n_batch", fallback=10
            )
            self.wait_interval = config.getfloat(
                "federated", "wait_interval", fallback=0.1
            )
            self.keep_optim_dict = config.getboolean(
                "federated", "keep_optim_dict", fallback=False
            )
            self.repetitions_dataset = config.getint(
                "federated", "repetitions_dataset", fallback=1
            )
            if self.repetitions_dataset > 1:
                self.epochs = int(self.epochs / self.repetitions_dataset)
                if verbose:
                    print(
                        "Number of epochs was decreased to "
                        "{:d} because of {:d} repetitions of dataset".format(
                            self.epochs, self.repetitions_dataset
                        )
                    )
            self.weighted_averaging = config.getboolean(
                "federated", "weighted_averaging", fallback=False
            )

        self.visdom = cmd_args.no_visdom if mode == "train" else False
        self.encrypted_inference = (
            cmd_args.encrypted_inference if mode == "inference" else False
        )
        self.dataset = cmd_args.dataset  # options: ['pneumonia', 'mnist']
        self.no_cuda = cmd_args.no_cuda
        self.websockets = cmd_args.websockets if mode == "train" else False
        if self.websockets:
            assert self.train_federated, "If you use websockets it must be federated"

    @classmethod
    def from_namespace(cls, args):
        obj = cls.__new__(cls)
        super(Arguments, obj).__init__()
        for attr in dir(args):
            if (
                not callable(getattr(args, attr))
                and not attr.startswith("__")
                and attr in dir(args)
            ):
                setattr(obj, attr, getattr(args, attr)) 
        return obj

    def from_previous_checkpoint(self, cmd_args):
        self.visdom = False
        self.encrypted_inference = cmd_args.encrypted_inference
        self.no_cuda = cmd_args.no_cuda
        self.websockets = (
            cmd_args.websockets  # currently not implemented for inference
            if self.encrypted_inference
            else False
        )
        if not "mixup" in dir(self):
            self.mixup = False

    def incorporate_cmd_args(self, cmd_args):
        exceptions = []  # just for future
        for attr in dir(self):
            if (
                not callable(getattr(self, attr))
                and not attr.startswith("__")
                and attr in dir(cmd_args)
                and attr not in exceptions
            ):
                setattr(self, attr, getattr(cmd_args, attr))

    def __str__(self):
        members = [
            attr
            for attr in dir(self)
            if not callable(getattr(self, attr)) and not attr.startswith("__")
        ]
        rows = []
        for x in members:
            rows.append([str(x), str(getattr(self, x))])
        return tabulate(rows)


class AddGaussianNoise(torch.nn.Module):
    def __init__(self, mean: float = 0.0, std: float = 1.0, p: Optional[float] = None):
        super(AddGaussianNoise, self).__init__()
        self.std = std
        self.mean = mean
        self.p = p

    def forward(self, tensor: torch.Tensor):
        if self.p and self.p < random():
            return tensor
        return (
            tensor
            + torch.randn(tensor.size()) * self.std  # pylint: disable=no-member
            + self.mean
        )

    def __repr__(self):
        return self.__class__.__name__ + "(mean={0}, std={1}{:s})".format(
            self.mean, self.std, ", apply prob={:f}".format(self.p) if self.p else ""
        )


class MixUp(torch.nn.Module):
    def __init__(self, λ: Optional[float] = None, p: Optional[float] = None):
        super(MixUp, self).__init__()
        assert 0.0 <= p <= 1.0, "probability needs to be in [0,1]"
        self.p = p
        if λ:
            assert 0.0 <= λ <= 1.0, "mix factor needs to be in [0,1]"
        self.λ = λ

    def forward(
        self, x: Tuple[Union[torch.tensor, Tuple[torch.tensor]], Tuple[torch.Tensor]],
    ):
        assert len(x) == 2, "need data and target"
        x, y = x
        if self.p:
            if random() > self.p:
                if torch.is_tensor(x):
                    return x, y
                else:
                    return x[0], y[0]
        if torch.is_tensor(x):
            L = x.shape[0]
        elif type(x) == tuple and all(
            [x[i].shape == x[i - 1].shape for i in range(1, len(x))]
        ):
            L = len(x)
        else:
            raise ValueError(
                "images need to be either list of equally shaped "
                "tensors or batch of size 2"
            )
        if not (
            (torch.is_tensor(y) and y.shape[0] == L)
            or (
                len(y) == L
                and all([y[i - 1].shape == y[i].shape for i in range(1, len(y))])
            )
        ):
            raise ValueError(
                "targets need to be tuple of equally shaped one hot encoded tensors"
            )
        if L == 1:
            return x, y
        if self.λ:
            λ = self.λ
        else:
            λ = random()
        if L % 2 == 0:
            h = L // 2
            if not torch.is_tensor(x):
                x = torch.stack(x).squeeze(1)  # pylint:disable=no-member
            if not torch.is_tensor(y):
                y = torch.stack(y).squeeze(1)  # pylint:disable=no-member
            x = λ * x[:h] + (1.0 - λ) * x[h:]
            y = λ * y[:h] + (1.0 - λ) * y[h:]
            return x, y
        else:
            # Actually there should be another distinction
            # between tensors and tuples
            # but in our use case this only happens if tensors
            # are used
            h = (L - 1) // 2
            out_x = torch.zeros(  # pylint:disable=no-member
                (h + 1, *x.shape[1:]), device=x.device
            )
            out_y = torch.zeros(  # pylint:disable=no-member
                (h + 1, *y.shape[1:]), device=y.device
            )
            out_x[-1] = x[-1]
            out_y[-1] = y[-1]
            out_x[:-1] = λ * x[:h] + (1.0 - λ) * x[h:-1]
            out_y[:-1] = λ * y[:h] + (1.0 - λ) * y[h:-1]
            return out_x, out_y


# adapted from https://discuss.pytorch.org/t/convert-int-into-one-hot-format/507/3
class Cross_entropy_one_hot(torch.nn.Module):
    def __init__(self, reduction="mean", weight=None):
        # Cross entropy that accepts soft targets
        super(Cross_entropy_one_hot, self).__init__()
        self.weight = (
            torch.nn.Parameter(weight, requires_grad=False)
            if weight is not None
            else None
        )
        self.logsoftmax = torch.nn.LogSoftmax(dim=1)
        if reduction == "mean":
            self.loss = lambda output, target: torch.mean(  # pylint:disable=no-member
                (
                    torch.sum(self.weight * target, dim=1)  # pylint:disable=no-member
                    if self.weight is not None
                    else 1.0
                )
                * torch.sum(  # pylint:disable=no-member
                    -target * self.logsoftmax(output), dim=1
                )
            )
        elif reduction == "sum":
            self.loss = lambda output, target: torch.sum(  # pylint:disable=no-member
                (
                    torch.sum(self.weight * target, dim=1)  # pylint:disable=no-member
                    if self.weight is not None
                    else 1.0
                )
                * torch.sum(  # pylint:disable=no-member
                    -target * self.logsoftmax(output), dim=1
                )
            )
        else:
            raise NotImplementedError("reduction method unknown")

    def forward(self, output: torch.Tensor, target: torch.Tensor):
        return self.loss(output, target)


class To_one_hot(torch.nn.Module):
    def __init__(self, num_classes):
        super(To_one_hot, self).__init__()
        self.num_classes = num_classes

    def forward(self, x: Union[int, List[int], torch.Tensor]):
        if type(x) == int:
            x = torch.tensor(x)  # pylint:disable=not-callable
        elif type(x) == list:
            x = torch.tensor(x)  # pylint:disable=not-callable
        if len(x.shape) == 0:
            one_hot = torch.zeros(  # pylint:disable=no-member
                (self.num_classes,), device=x.device
            )
            one_hot.scatter_(0, x, 1)
            return one_hot
        elif len(x.shape) == 1:
            x = x.unsqueeze(1)
        one_hot = torch.zeros(  # pylint:disable=no-member
            (x.shape[0], self.num_classes), device=x.device
        )
        one_hot.scatter_(1, x, 1)
        return one_hot


def save_config_results(args, roc_auc: float, timestamp: str, table: str):
    members = [
        attr
        for attr in dir(args)
        if not callable(getattr(args, attr)) and not attr.startswith("__")
    ]
    if not isfile(table):
        print("Configuration table does not exist - Creating new")
        df = pd.DataFrame(columns=members)
    else:
        df = pd.read_csv(table)
    new_row = dict(zip(members, [getattr(args, x) for x in members]))
    new_row["timestamp"] = timestamp
    new_row["best_validation_roc_auc"] = roc_auc
    df = df.append(new_row, ignore_index=True)
    df.to_csv(table, index=False)


## Adaption of federated averaging from syft with option of weights
def federated_avg(models: dict, weights: Optional[torch.Tensor] = None):
    """Calculate the federated average of a dictionary containing models.
       The models are extracted from the dictionary
       via the models.values() command.

    Args:
        models (Dict[Any, torch.nn.Module]): a dictionary of models
        for which the federated average is calculated.

    Returns:
        torch.nn.Module: the module with averaged parameters.
    """
    if weights:
        model = None
        for id, partial_model in models.items():
            scaled_model = scale_model(partial_model, weights[id])
            if model:
                model = add_model(model, scaled_model)
            else:
                model = scaled_model
    else:
        nr_models = len(models)
        model_list = list(models.values())
        model = model_list[0]
        for i in range(1, nr_models):
            model = add_model(model, model_list[i])
        model = scale_model(model, (1.0 / nr_models))
    return model


def training_animation(done: mp.Value, message: str = "training"):
    i = 0
    while not done.value:
        if i % 4 == 0:
            print("\r \033[K", end="{:s}".format(message), flush=True)
            i = 1
        else:
            print(".", end="", flush=True)
            i += 1
        sleep(0.5)
    print("\r \033[K")


def progress_animation(done, progress_dict):
    while not done.value:
        content, headers = [], []
        for worker, (batch, total) in progress_dict.items():
            headers.append(worker)
            content.append("{:d}/{:d}".format(batch, total))
        print(tabulate([content], headers=headers, tablefmt="plain"))
        sleep(0.1)
        print("\033[F" * 3)
    print("\033[K \n \033[K \033[F \033[F")


## Assuming train loaders is dictionary with {worker : train_loader}
def train_federated(
    args,
    model,
    device,
    train_loaders,
    optimizer,
    epoch,
    loss_fn,
    test_params=None,
    vis_params=None,
    verbose=True,
):
    model.train()
    mng = mp.Manager()
    result_dict, waiting_for_sync_dict, sync_dict, progress_dict, loss_dict = (
        mng.dict(),
        mng.dict(),
        mng.dict(),
        mng.dict(),
        mng.dict(),
    )
    stop_sync, sync_completed = mng.Value("i", False), mng.Value("i", False)
    # num_workers = mng.Value("d", len(train_loaders.keys()))
    for worker in train_loaders.keys():
        result_dict[worker.id] = None
        loss_dict[worker.id] = mng.dict()
        waiting_for_sync_dict[worker.id] = False
        progress_dict[worker.id] = (0, len(train_loaders[worker]))

    total_batches = 0
    weights = []
    for idt, (_, batches) in progress_dict.items():
        total_batches += batches
        weights.append(batches)
    weights = np.array(weights) / total_batches
    if args.weighted_averaging:
        w_dict = {}
        for weight, idt in zip(weights, progress_dict.keys()):
            w_dict[idt] = weight
    else:
        w_dict = {idt: 1.0 / len(progress_dict.keys()) for idt in progress_dict.keys()}
    jobs = [
        mp.Process(
            name="{:s} training".format(worker.id),
            target=train_on_server,
            args=(
                args,
                model,
                worker,
                device,
                train_loader,
                optimizer,
                loss_fn,
                result_dict,
                waiting_for_sync_dict,
                sync_dict,
                sync_completed,
                progress_dict,
                loss_dict,
            ),
        )
        for worker, train_loader in train_loaders.items()
    ]
    for j in jobs:
        j.start()
    synchronize = mp.Process(
        name="synchronization",
        target=synchronizer,
        args=(
            args,
            result_dict,
            waiting_for_sync_dict,
            sync_dict,
            progress_dict,
            loss_dict,
            stop_sync,
            sync_completed,
            w_dict,
            epoch,
        ),
        kwargs={
            "wait_interval": args.wait_interval,
            "vis_params": vis_params,
            "test_params": test_params,
        },
    )
    synchronize.start()
    if verbose:
        done = mng.Value("i", False)
        animate = mp.Process(
            name="animation", target=progress_animation, args=(done, progress_dict)
        )
        animate.start()
    for j in jobs:
        j.join()
    stop_sync.value = True
    synchronize.join()
    if verbose:
        done.value = True
        animate.join()

    model = sync_dict["model"]

    avg_loss = np.average([l["final"] for l in loss_dict.values()], weights=weights)

    if args.visdom:
        vis_params["vis"].line(
            X=np.asarray([epoch]),
            Y=np.asarray([avg_loss]),
            win="loss_win",
            name="train_loss",
            update="append",
            env=vis_params["vis_env"],
        )
    else:
        print("Train Epoch: {} \tLoss: {:.6f}".format(epoch, avg_loss,))
    return model


def synchronizer(
    args,
    result_dict,
    waiting_for_sync_dict,
    sync_dict,
    progress_dict,
    loss_dict,
    stop,
    sync_completed,
    weights,
    epoch,
    wait_interval=0.1,
    vis_params=None,
    test_params=None,
):
    if test_params:
        save_iter: int = 1
    while not stop.value:
        while not all(waiting_for_sync_dict.values()) and not stop.value:
            sleep(0.1)
        # print("synchronizing: models from {:s}".format(str(result_dict.keys())))
        if len(result_dict) == 1:
            for model in result_dict.values():
                sync_dict["model"] = model
        elif len(result_dict) == 0:
            pass
        else:
            models = {}
            for id, worker_model in result_dict.items():
                models[id] = worker_model
            avg_model = federated_avg(models, weights=weights)
            sync_dict["model"] = avg_model
        for k in waiting_for_sync_dict.keys():
            waiting_for_sync_dict[k] = False
        ## In theory we should clear the models here
        ## However, if one worker has more samples than any other worker,
        ## but has imbalanced data this destroys our training.
        ## By keeping the last model of each worker in the dict,
        ## we still assure that it's training is not lost
        # result_dict.clear()

        ## this should be commented in but it triggers some backend failure
        """
        progress = progress_dict.values()
        cur_batch = max([p[0] for p in progress])
        save_after = max([p[1] for p in progress]) / args.repetitions_dataset
        progress = sum([p[0] / p[1] for p in progress]) / len(progress)

        if vis_params:
            if progress >= 1.0:
                sync_completed.value = True
                continue
            avg_loss = np.mean(
                [l[cur_batch] for l in loss_dict.values() if cur_batch in l]
            )
            vis_params["vis"].line(
                X=np.asarray([epoch - 1 + progress]),
                Y=np.asarray([avg_loss]),
                win="loss_win",
                name="train_loss",
                update="append",
                env=vis_params["vis_env"],
            )
        if test_params and cur_batch > (save_after * save_iter):
            model = avg_model.copy()
            _, roc_auc = test(
                args,
                model,
                test_params["device"],
                test_params["val_loader"],
                epoch,
                test_params["loss_fn"],
                verbose=False,
                num_classes=test_params["num_classes"],
                vis_params=vis_params,
                class_names=test_params["class_names"],
            )
            model_path = "model_weights/{:s}_epoch_{:03d}.pt".format(
                test_params["exp_name"],
                epoch * (args.repetitions_dataset if args.repetitions_dataset else 1)
                + save_iter,
            )

            save_model(model, test_params["optimizer"], model_path, args, epoch)
            test_params["roc_auc_scores"].append(roc_auc)
            test_params["model_paths"].append(model_path)

            save_iter += 1"""
        sync_completed.value = True


def train_on_server(
    args,
    model,
    worker,
    device,
    train_loader,
    optim,
    loss_fn,
    result_dict,
    waiting_for_sync_dict,
    sync_dict,
    sync_completed,
    progress_dict,
    loss_dict,
):
    optimizer = optim.get_optim(worker.id)
    avg_loss = []
    model.send(worker)
    loss_fn = loss_fn.send(worker)
    L = len(train_loader)
    for batch_idx, (data, target) in enumerate(train_loader):
        progress_dict[worker.id] = (batch_idx, L)
        if (
            # batch_idx % int(0.1 * L) == 0 and batch_idx > 0
            batch_idx % args.sync_every_n_batch == 0
            and batch_idx > 0
        ):  # synchronize models
            model = model.get()
            result_dict[worker.id] = model
            loss_dict[worker.id][batch_idx] = avg_loss[-1]
            sync_completed.value = False
            waiting_for_sync_dict[worker.id] = True
            while not sync_completed.value:
                sleep(args.wait_interval)
            model.load_state_dict(sync_dict["model"].state_dict())
            if not args.keep_optim_dict:
                kwargs = {"lr": args.lr, "weight_decay": args.weight_decay}
                if args.optimizer == "Adam":
                    kwargs["betas"] = (args.beta1, args.beta2)
                optimizer.__init__(model.parameters(), **kwargs)
            model.train()
            model.send(worker)
        data, target = data.to(device), target.to(device)
        optimizer.zero_grad()
        output = model(data)
        loss = loss_fn(output, target)
        loss.backward()
        optimizer.step()
        loss = loss.get()
        avg_loss.append(loss.detach().cpu().item())
    model.get()
    loss_fn = loss_fn.get()
    result_dict[worker.id] = model
    loss_dict[worker.id]["final"] = np.mean(avg_loss)
    progress_dict[worker.id] = (batch_idx + 1, L)
    del waiting_for_sync_dict[worker.id]
    optim.optimizers[worker.id] = optimizer


def train(
    args,
    model,
    device,
    train_loader,
    optimizer,
    epoch,
    loss_fn,
    num_classes,
    vis_params=None,
    verbose=True,
):
    model.train()
    if args.mixup:
        mixup = MixUp(λ=args.mixup_lambda, p=args.mixup_prob)
        oh_converter = To_one_hot(num_classes)
        oh_converter.to(device)

    L = len(train_loader)
    div = 1.0 / float(L)
    avg_loss = []
    for batch_idx, (data, target) in tqdm.tqdm(
        enumerate(train_loader),
        leave=False,
        desc="training epoch {:d}".format(epoch),
        total=L + 1,
    ):
        data, target = data.to(device), target.to(device)
        if args.mixup:
            with torch.no_grad():
                target = oh_converter(target)
                data, target = mixup((data, target))
        optimizer.zero_grad()
        output = model(data)
        loss = loss_fn(output, target)
        loss.backward()
        optimizer.step()
        if batch_idx % args.log_interval == 0:
            if args.visdom:
                vis_params["vis"].line(
                    X=np.asarray([epoch + float(batch_idx) * div - 1]),
                    Y=np.asarray([loss.item()]),
                    win="loss_win",
                    name="train_loss",
                    update="append",
                    env=vis_params["vis_env"],
                )
            else:
                avg_loss.append(loss.item())
    if not args.visdom and verbose:
        print("Train Epoch: {} \tLoss: {:.6f}".format(epoch, np.mean(avg_loss),))
    return model


def test(
    args,
    model,
    device,
    val_loader,
    epoch,
    loss_fn,
    num_classes,
    verbose=True,
    vis_params=None,
    class_names=None,
):
    oh_converter = None
    if args.mixup or (args.train_federated and args.weight_classes):
        oh_converter = To_one_hot(num_classes)
        oh_converter.to(device)
    model.eval()
    test_loss, TP = 0, 0
    total_pred, total_target, total_scores = [], [], []
    with torch.no_grad():
        for data, target in (
            tqdm.tqdm(
                val_loader,
                total=len(val_loader),
                desc="testing epoch {:d}".format(epoch),
                leave=False,
            )
            if verbose
            else val_loader
        ):
            if not args.encrypted_inference:
                data = data.to(device)
                target = target.to(device)
            output = model(data)
            loss = loss_fn(output, oh_converter(target) if oh_converter else target)
            test_loss += loss.item()  # sum up batch loss
            total_scores.append(output)
            pred = output.argmax(dim=1)
            tgts = target.view_as(pred)
            total_pred.append(pred)
            total_target.append(tgts)
            equal = pred.eq(tgts)
            TP += (
                equal.sum().copy().get().float_precision().long().item()
                if args.encrypted_inference
                else equal.sum().item()
            )
    test_loss /= len(val_loader)
    if args.encrypted_inference:
        objective = 100.0 * TP / (len(val_loader) * args.test_batch_size)
        L = len(val_loader.dataset)
        if verbose:
            print(
                "Test set: Epoch: {:d} Average loss: {:.4f}, Recall: {}/{} ({:.0f}%)\n".format(
                    epoch, test_loss, TP, L, objective,
                ),
                # end="",
            )
    else:
        total_pred = torch.cat(total_pred).cpu().numpy()  # pylint: disable=no-member
        total_target = (
            torch.cat(total_target).cpu().numpy()  # pylint: disable=no-member
        )
        total_scores = (
            torch.cat(total_scores).cpu().numpy()  # pylint: disable=no-member
        )
        total_scores -= total_scores.min(axis=1)[:, np.newaxis]
        total_scores = total_scores / total_scores.sum(axis=1)[:, np.newaxis]
        conf_matrix = mt.confusion_matrix(total_target, total_pred)
        report = mt.classification_report(
            total_target, total_pred, output_dict=True, zero_division=0
        )
        roc_auc = mt.roc_auc_score(total_target, total_scores, multi_class="ovo")
        rows = []
        for i in range(conf_matrix.shape[0]):
            report_entry = report[str(i)]
            row = [
                class_names[i] if class_names else i,
                "{:.1f} %".format(report_entry["recall"] * 100.0),
                "{:.1f} %".format(report_entry["precision"] * 100.0),
                "{:.1f} %".format(report_entry["f1-score"] * 100.0),
                report_entry["support"],
            ]
            row.extend([conf_matrix[i, j] for j in range(conf_matrix.shape[1])])
            rows.append(row)
        rows.append(
            [
                "Overall (macro)",
                "{:.1f} %".format(report["macro avg"]["recall"] * 100.0),
                "{:.1f} %".format(report["macro avg"]["precision"] * 100.0),
                "{:.1f} %".format(report["macro avg"]["f1-score"] * 100.0),
                report["macro avg"]["support"],
            ]
        )
        rows.append(
            [
                "Overall (weighted)",
                "{:.1f} %".format(report["weighted avg"]["recall"] * 100.0),
                "{:.1f} %".format(report["weighted avg"]["precision"] * 100.0),
                "{:.1f} %".format(report["weighted avg"]["f1-score"] * 100.0),
                report["weighted avg"]["support"],
            ]
        )
        rows.append(
            ["Overall stats", "micro recall", "matthews coeff", "AUC ROC score"]
        )
        rows.append(
            [
                "",
                "{:.1f} %".format(100.0 * report["accuracy"]),
                "{:.3f}".format(mt.matthews_corrcoef(total_target, total_pred)),
                "{:.3f}".format(roc_auc),
            ]
        )
        objective = 100.0 * roc_auc
        headers = [
            "Epoch {:d}".format(epoch),
            "Recall",
            "Precision",
            "F1 score",
            "n total",
        ]
        headers.extend(
            [class_names[i] if class_names else i for i in range(conf_matrix.shape[0])]
        )
        if verbose:
            print(tabulate(rows, headers=headers, tablefmt="fancy_grid",))
        if args.visdom and vis_params:
            vis_params["vis"].line(
                X=np.asarray([epoch]),
                Y=np.asarray([objective / 100.0]),
                win="loss_win",
                name="ROC AUC",
                update="append",
                env=vis_params["vis_env"],
            )

    return test_loss, objective


def save_model(model, optim, path, args, epoch):
    opt_state_dict = (
        {name: optim.get_optim(name).state_dict() for name in optim.workers}
        if args.train_federated
        else optim.state_dict()
    )
    torch.save(
        {
            "epoch": epoch,
            "model_state_dict": model.state_dict(),
            "optim_state_dict": opt_state_dict,
            "args": args,
        },
        path,
    )
