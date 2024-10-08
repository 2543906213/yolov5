# Ultralytics YOLOv5 🚀, AGPL-3.0 license
"""
Train a YOLOv5 model on a custom dataset. Models and datasets download automatically from the latest YOLOv5 release.

Usage - Single-GPU training:
    $ python train.py --data coco128.yaml --weights yolov5s.pt --img 640  # from pretrained (recommended)
    $ python train.py --data coco128.yaml --weights '' --cfg yolov5s.yaml --img 640  # from scratch

Usage - Multi-GPU DDP training:
    $ python -m torch.distributed.run --nproc_per_node 4 --master_port 1 train.py --data coco128.yaml --weights yolov5s.pt --img 640 --device 0,1,2,3

Models:     https://github.com/ultralytics/yolov5/tree/master/models
Datasets:   https://github.com/ultralytics/yolov5/tree/master/data
Tutorial:   https://docs.ultralytics.com/yolov5/tutorials/train_custom_data
"""

from utils.torch_utils import (
    EarlyStopping,
    ModelEMA,
    de_parallel,
    select_device,
    smart_DDP,
    smart_optimizer,
    smart_resume,
    torch_distributed_zero_first,
)
from utils.plots import plot_evolve
from utils.metrics import fitness
from utils.loss import ComputeLoss
from utils.loggers.comet.comet_utils import check_comet_resume
from utils.loggers import LOGGERS, Loggers
from utils.general import (
    LOGGER,
    TQDM_BAR_FORMAT,
    check_amp,
    check_dataset,
    check_file,
    check_git_info,
    check_git_status,
    check_img_size,
    check_requirements,
    check_suffix,
    check_yaml,
    colorstr,
    get_latest_run,
    increment_path,
    init_seeds,
    intersect_dicts,
    labels_to_class_weights,
    labels_to_image_weights,
    methods,
    one_cycle,
    print_args,
    print_mutation,
    strip_optimizer,
    yaml_save,
)
from utils.downloads import attempt_download, is_url
from utils.dataloaders import create_dataloader
from utils.callbacks import Callbacks
from utils.autobatch import check_train_batch_size
from utils.autoanchor import check_anchors
from models.yolo import Model
from models.experimental import attempt_load
import val as validate  # for end-of-epoch mAP
import argparse
import math
import os
import random
import subprocess
import sys
import time
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path

try:
    import comet_ml  # must be imported before torch (if installed)
except ImportError:
    comet_ml = None

import numpy as np
import torch
import torch.distributed as dist
import torch.nn as nn
import yaml
from torch.optim import lr_scheduler
from tqdm import tqdm

FILE = Path(__file__).resolve()
ROOT = FILE.parents[0]  # YOLOv5 root directory
if str(ROOT) not in sys.path:
    sys.path.append(str(ROOT))  # add ROOT to PATH
ROOT = Path(os.path.relpath(ROOT, Path.cwd()))  # relative


# https://pytorch.org/docs/stable/elastic/run.html
LOCAL_RANK = int(os.getenv("LOCAL_RANK", -1))
# 从环境变量中获取一个名为 RANK 的值，并将其转换为整数。
# 如果环境变量 RANK 不存在，则使用默认值 -1。
RANK = int(os.getenv("RANK", -1))
WORLD_SIZE = int(os.getenv("WORLD_SIZE", 1))
GIT_INFO = check_git_info()


def train(hyp, opt, device, callbacks):
    # 函数的主要功能是管理数据集、模型架构、损失计算和优化步骤，以便在指定设备上训练 YOLOv5 模型。该函数不返回任何值。
    # 该函数接受四个参数：超参数 hyp，训练选项 opt，设备 device 和回调函数 callbacks。
    """
    Train a YOLOv5 model on a custom dataset using specified hyperparameters, options, and device, managing datasets,
    model architecture, loss computation, and optimizer steps.

    Args:
        hyp (str | dict): Path to the hyperparameters YAML file or a dictionary of hyperparameters.
        opt (argparse.Namespace): Parsed command-line arguments containing training options.
        device (torch.device): Device on which training occurs, e.g., 'cuda' or 'cpu'.
        callbacks (Callbacks): Callback functions for various training events.

    Returns:
        None

    Models and datasets download automatically from the latest YOLOv5 release.

        For more usage details, refer to:
        - Models: https://github.com/ultralytics/yolov5/tree/master/models
        - Datasets: https://github.com/ultralytics/yolov5/tree/master/data
        - Tutorial: https://docs.ultralytics.com/yolov5/tutorials/train_custom_data
    """
    save_dir, epochs, batch_size, weights, single_cls, evolve, data, cfg, resume, noval, nosave, workers, freeze = (
        Path(opt.save_dir),  # 保存训练结果的目录路径
        opt.epochs,  # 训练的总轮数
        opt.batch_size,  # 每个批次的大小
        opt.weights,  # 预训练权重的路径
        opt.single_cls,  # 是否将多类数据集训练为单类
        opt.evolve,  # 是否进行超参数进化
        opt.data,  # 数据集配置文件的路径
        opt.cfg,  # 模型配置文件的路径
        opt.resume,  # 是否从最近的训练中恢复
        opt.noval,  # 是否只在最后一个 epoch 进行验证
        opt.nosave,  # 是否只保存最终的检查点
        opt.workers,  # 数据加载器的最大工作进程数
        opt.freeze,  # 需要冻结的层
    )
    # 在训练开始之前运行 on_pretrain_routine_start 回调函数
    callbacks.run("on_pretrain_routine_start")

    # 为训练过程中保存权重文件创建一个目录，并定义两个文件路径用于存储最新和最佳的模型权重。
    w = save_dir / "weights"  # 权重文件将被保存的目录
    # 如果 evolve 为真，则创建 w 的父目录；否则，直接创建 w 目录。
    # mkdir 方法的参数 parents=True 表示如果父目录不存在，则会递归创建所有必需的父目录；
    # exist_ok=True 表示如果目录已经存在，则不会引发异常。
    (w.parent if evolve else w).mkdir(parents=True, exist_ok=True)
    # last, best表示最新模型权重文件和最佳模型权重文件的路径。
    last, best = w / "last.pt", w / "best.pt"

    # 加载和记录训练过程中使用的超参数，并将其保存到选项对象中，以便在训练检查点中使用。
    if isinstance(hyp, str):
        with open(hyp, errors="ignore") as f:
            hyp = yaml.safe_load(f)  # load hyps dict
            # 记录加载的超参数
    LOGGER.info(colorstr("hyperparameters: ") +
                ", ".join(f"{k}={v}" for k, v in hyp.items()))
    # 将超参数字典的副本赋值给 opt.hyp，以便在保存训练检查点时使用这些超参数。
    opt.hyp = hyp.copy()  # for saving hyps to checkpoints

    # 在训练过程中保存运行设置，包括超参数和选项参数
    if not evolve:
        yaml_save(save_dir / "hyp.yaml", hyp)
        yaml_save(save_dir / "opt.yaml", vars(opt))

    # 根据当前的运行环境和选项配置
    data_dict = None
    # 代码检查变量 RANK 是否在集合 {-1, 0} 中，如果是，则表示当前进程是主进程或单机运行，此时需要初始化日志记录器。
    if RANK in {-1, 0}:
        include_loggers = list(LOGGERS)  # 创建了一个包含默认日志记录器的列表
        if getattr(opt, "ndjson_console", False):
            include_loggers.append("ndjson_console")
        if getattr(opt, "ndjson_file", False):
            include_loggers.append("ndjson_file")

        # 用于管理和记录训练过程中的各种日志信息
        loggers = Loggers(
            save_dir=save_dir,
            weights=weights,
            opt=opt,
            hyp=hyp,
            logger=LOGGER,
            include=tuple(include_loggers),
        )

        # 注册日志记录器（loggers）中的方法作为回调函数
        for k in methods(loggers):
            callbacks.register_action(k, callback=getattr(loggers, k))

        # 处理自定义数据集的链接
        data_dict = loggers.remote_dataset
        if resume:  # 是否从远程存储恢复训练
            weights, epochs, hyp, batch_size = opt.weights, opt.epochs, opt.hyp, opt.batch_size

    # 配置训练过程中的一些关键参数和环境设置
    plots = not evolve and not opt.noplots  # 通过检查变量 evolve 和 opt.noplots 来决定是否生成绘图
    cuda = device.type != "cpu"
    # 初始化随机数生成器的种子，并设置确定性选项。
    init_seeds(opt.seed + 1 + RANK, deterministic=True)
    with torch_distributed_zero_first(LOCAL_RANK):  # 代码确保在分布式训练中按顺序执行操作。
        data_dict = data_dict or check_dataset(data)  # 验证或自动下载数据集，并返回其配置字典。
    train_path, val_path = data_dict["train"], data_dict["val"]
    nc = 1 if single_cls else int(data_dict["nc"])  # 获取类别数量并转换为整数
    names = {0: "item"} if single_cls and len(
        data_dict["names"]) != 1 else data_dict["names"]  # 确定类别名称
    is_coco = isinstance(val_path, str) and val_path.endswith(
        "coco/val2017.txt")  # 以确定数据集是否为 COCO 数据集，并将结果赋值给 is_coco 变量。

    # Model
    check_suffix(weights, ".pt")  # 检查权重文件的后缀是否为 .pt
    pretrained = weights.endswith(".pt")
    if pretrained:
        # 用于同步不同进程对数据读取的上下文管理器
        with torch_distributed_zero_first(LOCAL_RANK):
            # download if not found locally
            weights = attempt_download(weights)
        # load checkpoint to CPU to avoid CUDA memory leak
        ckpt = torch.load(weights, map_location="cpu")
        model = Model(cfg or ckpt["model"].yaml, ch=3, nc=nc, anchors=hyp.get(
            "anchors")).to(device)  # create
        exclude = ["anchor"] if (cfg or hyp.get(
            "anchors")) and not resume else []  # exclude keys
        # checkpoint state_dict as FP32
        csd = ckpt["model"].float().state_dict()
        csd = intersect_dicts(csd, model.state_dict(),
                              exclude=exclude)  # intersect
        model.load_state_dict(csd, strict=False)  # load
        LOGGER.info(
            f"Transferred {len(csd)}/{len(model.state_dict())} items from {weights}")  # report
    else:
        model = Model(cfg, ch=3, nc=nc, anchors=hyp.get(
            "anchors")).to(device)  # create
    amp = check_amp(model)  # check AMP

    # 冻结权重层
    freeze = [f"model.{x}." for x in (freeze if len(
        freeze) > 1 else range(freeze[0]))]  # layers to freeze
    for k, v in model.named_parameters():
        v.requires_grad = True  # train all layers
        # v.register_hook(lambda x: torch.nan_to_num(x))  # NaN to 0 (commented for erratic training results)
        if any(x in k for x in freeze):
            LOGGER.info(f"freezing {k}")
            v.requires_grad = False

    # Image size
    gs = max(int(model.stride.max()), 32)  # grid size (max stride)
    # verify imgsz is gs-multiple
    imgsz = check_img_size(opt.imgsz, gs, floor=gs * 2)

    # Batch size
    if RANK == -1 and batch_size == -1:  # single-GPU only, estimate best batch size
        batch_size = check_train_batch_size(model, imgsz, amp)
        loggers.on_params_update({"batch_size": batch_size})

    # Optimizer
    nbs = 64  # nominal batch size
    # accumulate loss before optimizing
    accumulate = max(round(nbs / batch_size), 1)
    hyp["weight_decay"] *= batch_size * accumulate / nbs  # scale weight_decay
    optimizer = smart_optimizer(
        model, opt.optimizer, hyp["lr0"], hyp["momentum"], hyp["weight_decay"])

    # Scheduler
    if opt.cos_lr:
        lf = one_cycle(1, hyp["lrf"], epochs)  # cosine 1->hyp['lrf']
    else:

        def lf(x):
            """Linear learning rate scheduler function with decay calculated by epoch proportion."""
            return (1 - x / epochs) * (1.0 - hyp["lrf"]) + hyp["lrf"]  # linear

    # plot_lr_scheduler(optimizer, scheduler, epochs)
    scheduler = lr_scheduler.LambdaLR(optimizer, lr_lambda=lf)

    # EMA
    ema = ModelEMA(model) if RANK in {-1, 0} else None

    # Resume
    best_fitness, start_epoch = 0.0, 0
    if pretrained:
        if resume:
            best_fitness, start_epoch, epochs = smart_resume(
                ckpt, optimizer, ema, weights, epochs, resume)
        del ckpt, csd

    # DP mode
    if cuda and RANK == -1 and torch.cuda.device_count() > 1:
        LOGGER.warning(
            "WARNING ⚠️ DP not recommended, use torch.distributed.run for best DDP Multi-GPU results.\n"
            "See Multi-GPU Tutorial at https://docs.ultralytics.com/yolov5/tutorials/multi_gpu_training to get started."
        )
        model = torch.nn.DataParallel(model)

    # SyncBatchNorm
    if opt.sync_bn and cuda and RANK != -1:
        model = torch.nn.SyncBatchNorm.convert_sync_batchnorm(model).to(device)
        LOGGER.info("Using SyncBatchNorm()")

    # Trainloader
    train_loader, dataset = create_dataloader(
        train_path,
        imgsz,
        batch_size // WORLD_SIZE,
        gs,
        single_cls,
        hyp=hyp,
        augment=True,
        cache=None if opt.cache == "val" else opt.cache,
        rect=opt.rect,
        rank=LOCAL_RANK,
        workers=workers,
        image_weights=opt.image_weights,
        quad=opt.quad,
        prefix=colorstr("train: "),
        shuffle=True,
        seed=opt.seed,
    )
    labels = np.concatenate(dataset.labels, 0)
    mlc = int(labels[:, 0].max())  # max label class
    assert mlc < nc, f"Label class {mlc} exceeds nc={nc} in {data}. Possible class labels are 0-{nc - 1}"

    # Process 0
    if RANK in {-1, 0}:
        val_loader = create_dataloader(
            val_path,
            imgsz,
            batch_size // WORLD_SIZE * 2,
            gs,
            single_cls,
            hyp=hyp,
            cache=None if noval else opt.cache,
            rect=True,
            rank=-1,
            workers=workers * 2,
            pad=0.5,
            prefix=colorstr("val: "),
        )[0]

        if not resume:
            if not opt.noautoanchor:
                # run AutoAnchor
                check_anchors(dataset, model=model,
                              thr=hyp["anchor_t"], imgsz=imgsz)
            model.half().float()  # pre-reduce anchor precision

        callbacks.run("on_pretrain_routine_end", labels, names)

    # DDP mode
    if cuda and RANK != -1:
        model = smart_DDP(model)

    # Model attributes
    # number of detection layers (to scale hyps)
    nl = de_parallel(model).model[-1].nl
    hyp["box"] *= 3 / nl  # scale to layers
    hyp["cls"] *= nc / 80 * 3 / nl  # scale to classes and layers
    hyp["obj"] *= (imgsz / 640) ** 2 * 3 / nl  # scale to image size and layers
    hyp["label_smoothing"] = opt.label_smoothing
    model.nc = nc  # attach number of classes to model
    model.hyp = hyp  # attach hyperparameters to model
    model.class_weights = labels_to_class_weights(
        dataset.labels, nc).to(device) * nc  # attach class weights
    model.names = names

    # Start training
    t0 = time.time()
    nb = len(train_loader)  # number of batches
    # number of warmup iterations, max(3 epochs, 100 iterations)
    nw = max(round(hyp["warmup_epochs"] * nb), 100)
    # nw = min(nw, (epochs - start_epoch) / 2 * nb)  # limit warmup to < 1/2 of training
    last_opt_step = -1
    maps = np.zeros(nc)  # mAP per class
    # P, R, mAP@.5, mAP@.5-.95, val_loss(box, obj, cls)
    results = (0, 0, 0, 0, 0, 0, 0)
    scheduler.last_epoch = start_epoch - 1  # do not move
    scaler = torch.cuda.amp.GradScaler(enabled=amp)
    stopper, stop = EarlyStopping(patience=opt.patience), False
    compute_loss = ComputeLoss(model)  # init loss class
    callbacks.run("on_train_start")
    LOGGER.info(
        f'Image sizes {imgsz} train, {imgsz} val\n'
        f'Using {train_loader.num_workers * WORLD_SIZE} dataloader workers\n'
        f"Logging results to {colorstr('bold', save_dir)}\n"
        f'Starting training for {epochs} epochs...'
    )
    # epoch ------------------------------------------------------------------
    for epoch in range(start_epoch, epochs):
        callbacks.run("on_train_epoch_start")
        model.train()

        # Update image weights (optional, single-GPU only)
        if opt.image_weights:
            cw = model.class_weights.cpu().numpy() * (1 - maps) ** 2 / nc  # class weights
            iw = labels_to_image_weights(
                dataset.labels, nc=nc, class_weights=cw)  # image weights
            dataset.indices = random.choices(
                range(dataset.n), weights=iw, k=dataset.n)  # rand weighted idx

        # Update mosaic border (optional)
        # b = int(random.uniform(0.25 * imgsz, 0.75 * imgsz + gs) // gs * gs)
        # dataset.mosaic_border = [b - imgsz, -b]  # height, width borders

        mloss = torch.zeros(3, device=device)  # mean losses
        if RANK != -1:
            train_loader.sampler.set_epoch(epoch)
        pbar = enumerate(train_loader)
        LOGGER.info(("\n" + "%11s" * 7) % ("Epoch", "GPU_mem",
                    "box_loss", "obj_loss", "cls_loss", "Instances", "Size"))
        if RANK in {-1, 0}:
            # progress bar
            pbar = tqdm(pbar, total=nb, bar_format=TQDM_BAR_FORMAT)
        optimizer.zero_grad()
        # batch -------------------------------------------------------------
        for i, (imgs, targets, paths, _) in pbar:
            callbacks.run("on_train_batch_start")
            # number integrated batches (since train start)
            ni = i + nb * epoch
            imgs = imgs.to(device, non_blocking=True).float() / \
                255  # uint8 to float32, 0-255 to 0.0-1.0

            # Warmup
            if ni <= nw:
                xi = [0, nw]  # x interp
                # compute_loss.gr = np.interp(ni, xi, [0.0, 1.0])  # iou loss ratio (obj_loss = 1.0 or iou)
                accumulate = max(1, np.interp(
                    ni, xi, [1, nbs / batch_size]).round())
                for j, x in enumerate(optimizer.param_groups):
                    # bias lr falls from 0.1 to lr0, all other lrs rise from 0.0 to lr0
                    x["lr"] = np.interp(
                        ni, xi, [hyp["warmup_bias_lr"] if j == 0 else 0.0, x["initial_lr"] * lf(epoch)])
                    if "momentum" in x:
                        x["momentum"] = np.interp(
                            ni, xi, [hyp["warmup_momentum"], hyp["momentum"]])

            # Multi-scale
            if opt.multi_scale:
                sz = random.randrange(
                    int(imgsz * 0.5), int(imgsz * 1.5) + gs) // gs * gs  # size
                sf = sz / max(imgs.shape[2:])  # scale factor
                if sf != 1:
                    # new shape (stretched to gs-multiple)
                    ns = [math.ceil(x * sf / gs) * gs for x in imgs.shape[2:]]
                    imgs = nn.functional.interpolate(
                        imgs, size=ns, mode="bilinear", align_corners=False)

            # Forward
            with torch.cuda.amp.autocast(amp):
                pred = model(imgs)  # forward
                loss, loss_items = compute_loss(
                    pred, targets.to(device))  # loss scaled by batch_size
                if RANK != -1:
                    loss *= WORLD_SIZE  # gradient averaged between devices in DDP mode
                if opt.quad:
                    loss *= 4.0

            # Backward
            scaler.scale(loss).backward()

            # Optimize - https://pytorch.org/docs/master/notes/amp_examples.html
            if ni - last_opt_step >= accumulate:
                scaler.unscale_(optimizer)  # unscale gradients
                torch.nn.utils.clip_grad_norm_(
                    model.parameters(), max_norm=10.0)  # clip gradients
                scaler.step(optimizer)  # optimizer.step
                scaler.update()
                optimizer.zero_grad()
                if ema:
                    ema.update(model)
                last_opt_step = ni

            # Log
            if RANK in {-1, 0}:
                mloss = (mloss * i + loss_items) / \
                    (i + 1)  # update mean losses
                # (GB)
                mem = f"{torch.cuda.memory_reserved() / 1E9 if torch.cuda.is_available() else 0:.3g}G"
                pbar.set_description(
                    ("%11s" * 2 + "%11.4g" * 5)
                    % (f"{epoch}/{epochs - 1}", mem, *mloss, targets.shape[0], imgs.shape[-1])
                )
                callbacks.run("on_train_batch_end", model, ni,
                              imgs, targets, paths, list(mloss))
                if callbacks.stop_training:
                    return
            # end batch ------------------------------------------------------------------------------------------------

        # Scheduler
        lr = [x["lr"] for x in optimizer.param_groups]  # for loggers
        scheduler.step()

        if RANK in {-1, 0}:
            # mAP
            callbacks.run("on_train_epoch_end", epoch=epoch)
            ema.update_attr(
                model, include=["yaml", "nc", "hyp", "names", "stride", "class_weights"])
            final_epoch = (epoch + 1 == epochs) or stopper.possible_stop
            if not noval or final_epoch:  # Calculate mAP
                results, maps, _ = validate.run(
                    data_dict,
                    batch_size=batch_size // WORLD_SIZE * 2,
                    imgsz=imgsz,
                    half=amp,
                    model=ema.ema,
                    single_cls=single_cls,
                    dataloader=val_loader,
                    save_dir=save_dir,
                    plots=False,
                    callbacks=callbacks,
                    compute_loss=compute_loss,
                )

            # Update best mAP
            # weighted combination of [P, R, mAP@.5, mAP@.5-.95]
            fi = fitness(np.array(results).reshape(1, -1))
            stop = stopper(epoch=epoch, fitness=fi)  # early stop check
            if fi > best_fitness:
                best_fitness = fi
            log_vals = list(mloss) + list(results) + lr
            callbacks.run("on_fit_epoch_end", log_vals,
                          epoch, best_fitness, fi)

            # Save model
            if (not nosave) or (final_epoch and not evolve):  # if save
                ckpt = {
                    "epoch": epoch,
                    "best_fitness": best_fitness,
                    "model": deepcopy(de_parallel(model)).half(),
                    "ema": deepcopy(ema.ema).half(),
                    "updates": ema.updates,
                    "optimizer": optimizer.state_dict(),
                    "opt": vars(opt),
                    "git": GIT_INFO,  # {remote, branch, commit} if a git repo
                    "date": datetime.now().isoformat(),
                }

                # Save last, best and delete
                torch.save(ckpt, last)
                if best_fitness == fi:
                    torch.save(ckpt, best)
                if opt.save_period > 0 and epoch % opt.save_period == 0:
                    torch.save(ckpt, w / f"epoch{epoch}.pt")
                del ckpt
                callbacks.run("on_model_save", last, epoch,
                              final_epoch, best_fitness, fi)

        # EarlyStopping
        if RANK != -1:  # if DDP training
            broadcast_list = [stop if RANK == 0 else None]
            # broadcast 'stop' to all ranks
            dist.broadcast_object_list(broadcast_list, 0)
            if RANK != 0:
                stop = broadcast_list[0]
        if stop:
            break  # must break all DDP ranks

        # end epoch ----------------------------------------------------------------------------------------------------
    # end training -----------------------------------------------------------------------------------------------------
    if RANK in {-1, 0}:
        LOGGER.info(
            f"\n{epoch - start_epoch + 1} epochs completed in {(time.time() - t0) / 3600:.3f} hours.")
        for f in last, best:
            if f.exists():
                strip_optimizer(f)  # strip optimizers
                if f is best:
                    LOGGER.info(f"\nValidating {f}...")
                    results, _, _ = validate.run(
                        data_dict,
                        batch_size=batch_size // WORLD_SIZE * 2,
                        imgsz=imgsz,
                        model=attempt_load(f, device).half(),
                        iou_thres=0.65 if is_coco else 0.60,  # best pycocotools at iou 0.65
                        single_cls=single_cls,
                        dataloader=val_loader,
                        save_dir=save_dir,
                        save_json=is_coco,
                        verbose=True,
                        plots=plots,
                        callbacks=callbacks,
                        compute_loss=compute_loss,
                    )  # val best model with plots
                    if is_coco:
                        callbacks.run("on_fit_epoch_end", list(
                            mloss) + list(results) + lr, epoch, best_fitness, fi)

        callbacks.run("on_train_end", last, best, epoch, results)

    torch.cuda.empty_cache()
    return results

# =======================================================================================================================

# 这个函数是用来解析命令行参数的，返回一个argparse.Namespace对象，包含了YOLOv5执行的选项
# known意思是


def parse_opt(known=False):
    # 第一行：一个用于解析 YOLOv5 训练、验证和测试的命令行参数的功能
    # 第二行：如果 known 设置为 True，函数将只解析已知的命令行参数，忽略未知的参数。
    # 如果调用函数时没有提供 known 参数，它将默认设置为 False。
    # 这意味着函数在默认情况下会解析所有参数，包括未知的参数。
    # 第三行：函数返回一个 argparse.Namespace 对象。
    # argparse.Namespace 是 Python 标准库 argparse 模块中的一个类，用于存储解析后的命令行参数。
    # 解析后的参数存储在 argparse.Namespace 对象中，用户可以通过访问该对象的属性来获取具体的参数值。
    """
    Parse command-line arguments for YOLOv5 training, validation, and testing.

    Args:
        known (bool, optional): If True, parses known arguments, ignoring the unknown. Defaults to False.

    Returns:
        (argparse.Namespace): Parsed command-line arguments containing options for YOLOv5 execution.

    Example:
        ```python
        from ultralytics.yolo import parse_opt
        opt = parse_opt()
        print(opt)
        ```

    Links:
        - Models: https://github.com/ultralytics/yolov5/tree/master/models
        - Datasets: https://github.com/ultralytics/yolov5/tree/master/data
        - Tutorial: https://docs.ultralytics.com/yolov5/tutorials/train_custom_data
    """

    # 代码实例化了一个 ArgumentParser 对象，并将其赋值给变量 parser。
    parser = argparse.ArgumentParser()
    # 代码添加了一些命令行参数，用于配置 YOLOv5 的训练和验证过程。
    # 代码中的每个 add_argument() 方法都会添加一个命令行参数。
    parser.add_argument("--weights", type=str, default=ROOT /
                        "yolov5s.pt", help="initial weights path")
    parser.add_argument("--cfg", type=str, default="", help="model.yaml path")
    parser.add_argument("--data", type=str, default=ROOT /
                        "data/coco128.yaml", help="dataset.yaml path")
    parser.add_argument("--hyp", type=str, default=ROOT /
                        "data/hyps/hyp.scratch-low.yaml", help="hyperparameters path")
    parser.add_argument("--epochs", type=int, default=100,
                        help="total training epochs")
    parser.add_argument("--batch-size", type=int, default=16,
                        help="total batch size for all GPUs, -1 for autobatch")
    parser.add_argument("--imgsz", "--img", "--img-size", type=int,
                        default=640, help="train, val image size (pixels)")
    parser.add_argument("--rect", action="store_true",
                        help="rectangular training")
    parser.add_argument("--resume", nargs="?", const=True,
                        default=False, help="resume most recent training")
    parser.add_argument("--nosave", action="store_true",
                        help="only save final checkpoint")
    parser.add_argument("--noval", action="store_true",
                        help="only validate final epoch")
    parser.add_argument("--noautoanchor", action="store_true",
                        help="disable AutoAnchor")
    parser.add_argument("--noplots", action="store_true",
                        help="save no plot files")
    parser.add_argument("--evolve", type=int, nargs="?", const=300,
                        help="evolve hyperparameters for x generations")
    parser.add_argument(
        "--evolve_population", type=str, default=ROOT / "data/hyps", help="location for loading population"
    )
    parser.add_argument("--resume_evolve", type=str, default=None,
                        help="resume evolve from last generation")
    parser.add_argument("--bucket", type=str, default="", help="gsutil bucket")
    parser.add_argument("--cache", type=str, nargs="?",
                        const="ram", help="image --cache ram/disk")
    parser.add_argument("--image-weights", action="store_true",
                        help="use weighted image selection for training")
    parser.add_argument("--device", default="",
                        help="cuda device, i.e. 0 or 0,1,2,3 or cpu")
    parser.add_argument("--multi-scale", action="store_true",
                        help="vary img-size +/- 50%%")
    parser.add_argument("--single-cls", action="store_true",
                        help="train multi-class data as single-class")
    parser.add_argument("--optimizer", type=str,
                        choices=["SGD", "Adam", "AdamW"], default="SGD", help="optimizer")
    parser.add_argument("--sync-bn", action="store_true",
                        help="use SyncBatchNorm, only available in DDP mode")
    parser.add_argument("--workers", type=int, default=8,
                        help="max dataloader workers (per RANK in DDP mode)")
    parser.add_argument("--project", default=ROOT /
                        "runs/train", help="save to project/name")
    parser.add_argument("--name", default="exp", help="save to project/name")
    parser.add_argument("--exist-ok", action="store_true",
                        help="existing project/name ok, do not increment")
    parser.add_argument("--quad", action="store_true", help="quad dataloader")
    parser.add_argument("--cos-lr", action="store_true",
                        help="cosine LR scheduler")
    parser.add_argument("--label-smoothing", type=float,
                        default=0.0, help="Label smoothing epsilon")
    parser.add_argument("--patience", type=int, default=100,
                        help="EarlyStopping patience (epochs without improvement)")
    parser.add_argument("--freeze", nargs="+", type=int,
                        default=[0], help="Freeze layers: backbone=10, first3=0 1 2")
    parser.add_argument("--save-period", type=int, default=-1,
                        help="Save checkpoint every x epochs (disabled if < 1)")
    parser.add_argument("--seed", type=int, default=0,
                        help="Global training seed")
    parser.add_argument("--local_rank", type=int, default=-1,
                        help="Automatic DDP Multi-GPU argument, do not modify")

    # Logger arguments 日志记录参数
    parser.add_argument("--entity", default=None, help="Entity")
    parser.add_argument("--upload_dataset", nargs="?", const=True,
                        default=False, help='Upload data, "val" option')
    parser.add_argument("--bbox_interval", type=int, default=-1,
                        help="Set bounding-box image logging interval")
    parser.add_argument("--artifact_alias", type=str,
                        default="latest", help="Version of dataset artifact to use")

    # NDJSON logging 日志记录  NDJSON 是一种日志记录格式，用于记录结构化数据
    parser.add_argument("--ndjson-console",
                        action="store_true", help="Log ndjson to console")
    parser.add_argument("--ndjson-file", action="store_true",
                        help="Log ndjson to file")

    # 根据 known 参数的值来决定调用哪种解析方法。
    # 如果 known 为 True，调用 parse_known_args() 方法解析已知的参数。
    # parse_known_args() 方法解析已知的命令行参数，并返回一个包含两个元素的元组：
    # 第一个元素是解析后的参数对象（Namespace），第二个元素是未解析的参数列表。
    # 通过 [0] 索引，只返回解析后的参数对象。这种方式允许脚本忽略未知的命令行参数，而不会因为未知参数而报错。
    # 如果 known 为 False，调用 parse_args() 方法解析所有参数。
    # parse_args() 方法解析所有命令行参数，并在遇到未知参数时抛出错误。
    # 这种方式确保所有传递给脚本的参数都是预定义的和已知的，从而避免意外的参数输入。
    # 通过这种方式，代码实现了灵活的命令行参数解析机制：
    # 当 known 为 True 时，脚本会忽略未知的参数，只解析已知的参数。
    # 这在某些情况下非常有用，例如当脚本需要与其他工具或脚本集成时，可能会接收到一些不相关的参数。
    # 当 known 为 False 时，脚本会严格解析所有参数，并在遇到未知参数时报错。这确保了参数输入的准确性和一致性。
    return parser.parse_known_args()[0] if known else parser.parse_args()

# =======================================================================================================================


def main(opt, callbacks=Callbacks()):

    # 使用指定的选项和可选的回调函数运行训练或超参数进化的主要入口点。
    """
    Runs the main entry point for training or hyperparameter evolution with specified options and optional callbacks.

    Args:
    # opt 是一个 argparse.Namespace 对象，包含了 YOLOv5 训练和进化的命令行参数。
    # callbacks 是一个 Callbacks 对象，包含了各个训练阶段的回调函数。
        opt (argparse.Namespace): The command-line arguments parsed for YOLOv5 training and evolution.
        callbacks (ultralytics.utils.callbacks.Callbacks, optional): Callback functions for various training stages.
            Defaults to Callbacks().

    Returns:
        None

    Note:
        For detailed usage, refer to:
        https://github.com/ultralytics/yolov5/tree/master/models
    """

    # 检查变量 RANK 是否在集合 {-1, 0} 中。
    # RANK 通常用于表示当前进程的排名或身份。
    # 例如，在分布式训练中，RANK 为 0 的进程通常是主进程，而其他进程则是辅助进程。
    # 当 RANK 为 -1 或 0 时，表示当前进程是主进程或单进程模式，因此需要执行以下操作。
    if RANK in {-1, 0}:
        # 用于记录调用函数的参数。
        # vars(opt) 将 opt 对象转换为字典，包含所有命令行解析后的参数。
        # 这行代码的作用是打印或记录当前训练或进化过程的所有参数，方便调试和日志记录。
        print_args(vars(opt))
        # 用于检查当前代码库是否与远程仓库同步。
        # 该函数会检查当前代码库是否是一个 Git 仓库，是否在线，并且是否有未拉取的更新。
        # 如果代码库有更新，它会提示用户运行 git pull 命令来更新代码库。
        check_git_status()
        # 用于检查当前环境中的依赖项是否满足要求。
        # 该函数会读取 requirements.txt 文件中的依赖项，并检查这些依赖项是否已经安装且版本符合要求。
        # 如果某些依赖项未安装或版本不符合要求，它会尝试自动安装或更新这些依赖项。
        check_requirements(ROOT / "requirements.txt")

    # Resume (from specified or most recent last.pt)
    # 检查 opt.resume 是否为 True，如果是，则表示需要恢复最近的训练检查点。
    # 这段代码处理了训练过程中的恢复选项，决定是否从之前的检查点恢复训练。
    # opt.resume 是一个命令行选项，用于指示是否从之前的检查点恢复训练。
    # 在训练深度学习模型时，恢复训练可以节省时间和资源，因为可以从上次中断的地方继续，而不必从头开始。
    '''首先检查是否需要恢复训练。条件包括 opt.resume 为真，
     且 check_comet_resume(opt) 和 opt.evolve 都为假。'''
    if opt.resume and not check_comet_resume(opt) and not opt.evolve:
        '''确定恢复的检查点文件路径。
        如果 opt.resume 是字符串，则使用 check_file(opt.resume) 获取文件路径；
        否则，调用 get_latest_run() 获取最近的运行记录。'''
        last = Path(check_file(opt.resume) if isinstance(
            opt.resume, str) else get_latest_run())
        # 构建一个指向 opt.yaml 文件的路径，并将其存储在变量 opt_yaml 中
        opt_yaml = last.parent.parent / "opt.yaml"  # train options yaml
        opt_data = opt.data  # original dataset
        if opt_yaml.is_file():
            # errors="ignore" 参数用于忽略文件读取过程中可能出现的编码错误。
            with open(opt_yaml, errors="ignore") as f:
                # 使用 yaml.safe_load(f) 将 YAML 文件的内容加载到字典 d 中
                d = yaml.safe_load(f)
        else:
            # 从一个保存的 PyTorch 模型文件中加载特定的配置选项，并将其存储在变量 d 中
            # map_location="cpu" 参数指定了将所有加载的张量映射到 CPU 上。
            # 这在没有 GPU 或不需要使用 GPU 时非常有用，可以避免 GPU 内存的占用。
            d = torch.load(last, map_location="cpu")["opt"]
            # **d 是一种特殊的语法，称为字典解包（dictionary unpacking）
            # 将字典 d 转换为一个命名空间对象
        opt = argparse.Namespace(**d)  # replace
        # reinstate 对opt里面的这三个参数进行重新赋值
        opt.cfg, opt.weights, opt.resume = "", str(last), True
        if is_url(opt_data):
            opt.data = check_file(opt_data)  # avoid HUB resume auth timeout
    else:
        opt.data, opt.cfg, opt.hyp, opt.weights, opt.project = (
            check_file(opt.data),
            check_yaml(opt.cfg),
            check_yaml(opt.hyp),
            str(opt.weights),
            str(opt.project),
        )  # checks
        # 通过断言语句确保在运行脚本时，配置文件 (cfg) 或权重文件 (weights) 至少有一个被指定。
        # 断言语句的作用是检查一个条件，如果条件为假，则抛出一个 AssertionError 异常，并显示指定的错误消息。
        assert len(opt.cfg) or len(
            opt.weights),   "either --cfg or --weights must be specified"
        if opt.evolve:
            if opt.project == str(ROOT / "runs/train"):
                opt.project = str(ROOT / "runs/evolve")
            # 确保在进化模式下，项目路径可以被覆盖，但不会继续之前的训练。
            opt.exist_ok, opt.resume = opt.resume, False
        if opt.name == "cfg":
            # stem 是 Path 类的一个属性，它返回路径的最后一个组成部分（即文件名），但不包括文件扩展名。
            # 例如，如果 opt.cfg 的值是 "config.yaml"，则 Path(opt.cfg).stem 的值将是 "config"。
            opt.name = Path(opt.cfg).stem  # use model.yaml as name
        opt.save_dir = str(increment_path(
            Path(opt.project) / opt.name, exist_ok=opt.exist_ok))

    # DDP mode
    # 分布式数据并行（DDP）模式
    # 选择计算设备（CPU、CUDA GPU 或 MPS）。
    # 这个函数会根据可用的硬件资源和用户指定的设备来选择最合适的设备，并返回一个 torch.device 对象。
    device = select_device(opt.device, batch_size=opt.batch_size)
    # 如果 LOCAL_RANK 不等于 -1，则表示当前进程是一个 DDP 进程。
    if LOCAL_RANK != -1:
        msg = "is not compatible with YOLOv5 Multi-GPU DDP training"
        assert not opt.image_weights, f"--image-weights {msg}"
        assert not opt.evolve, f"--evolve {msg}"
        assert opt.batch_size != - \
            1, f"AutoBatch with --batch-size -1 {msg}, please pass a valid --batch-size"
        assert opt.batch_size % WORLD_SIZE == 0, f"--batch-size {opt.batch_size} must be multiple of WORLD_SIZE"
        assert torch.cuda.device_count() > LOCAL_RANK, "insufficient CUDA devices for DDP command"
        torch.cuda.set_device(LOCAL_RANK)
        device = torch.device("cuda", LOCAL_RANK)
        dist.init_process_group(
            backend="nccl" if dist.is_nccl_available() else "gloo", timeout=timedelta(seconds=10800)
        )

    # Train 函数负责整个训练过程，包括数据集管理、模型架构、损失计算和优化步骤。
    # 它接受一些参数，包括opt.hyp超参数、opt包含训练选项和参数的对象、device设备、callbacks回调函数等。
    if not opt.evolve:
        train(opt.hyp, opt, device, callbacks)

    # Evolve hyperparameters (optional)
    else:
        # 字典meta，用于存储超参数进化的元数据
        # Hyperparameter evolution metadata (including this hyperparameter True-False, lower_limit, upper_limit)
        meta = {
            # initial learning rate (SGD=1E-2, Adam=1E-3)
            #
            "lr0": (False, 1e-5, 1e-1),
            # final OneCycleLR learning rate (lr0 * lrf)
            "lrf": (False, 0.01, 1.0),
            "momentum": (False, 0.6, 0.98),  # SGD momentum/Adam beta1
            "weight_decay": (False, 0.0, 0.001),  # optimizer weight decay
            "warmup_epochs": (False, 0.0, 5.0),  # warmup epochs (fractions ok)
            "warmup_momentum": (False, 0.0, 0.95),  # warmup initial momentum
            "warmup_bias_lr": (False, 0.0, 0.2),  # warmup initial bias lr
            "box": (False, 0.02, 0.2),  # box loss gain
            "cls": (False, 0.2, 4.0),  # cls loss gain
            "cls_pw": (False, 0.5, 2.0),  # cls BCELoss positive_weight
            "obj": (False, 0.2, 4.0),  # obj loss gain (scale with pixels)
            "obj_pw": (False, 0.5, 2.0),  # obj BCELoss positive_weight
            "iou_t": (False, 0.1, 0.7),  # IoU training threshold
            "anchor_t": (False, 2.0, 8.0),  # anchor-multiple threshold
            # anchors per output grid (0 to ignore)
            "anchors": (False, 2.0, 10.0),
            # focal loss gamma (efficientDet default gamma=1.5)
            "fl_gamma": (False, 0.0, 2.0),
            "hsv_h": (True, 0.0, 0.1),  # image HSV-Hue augmentation (fraction)
            # image HSV-Saturation augmentation (fraction)
            "hsv_s": (True, 0.0, 0.9),
            # image HSV-Value augmentation (fraction)
            "hsv_v": (True, 0.0, 0.9),
            "degrees": (True, 0.0, 45.0),  # image rotation (+/- deg)
            "translate": (True, 0.0, 0.9),  # image translation (+/- fraction)
            "scale": (True, 0.0, 0.9),  # image scale (+/- gain)
            "shear": (True, 0.0, 10.0),  # image shear (+/- deg)
            # image perspective (+/- fraction), range 0-0.001
            "perspective": (True, 0.0, 0.001),
            "flipud": (True, 0.0, 1.0),  # image flip up-down (probability)
            "fliplr": (True, 0.0, 1.0),  # image flip left-right (probability)
            "mosaic": (True, 0.0, 1.0),  # image mixup (probability)
            "mixup": (True, 0.0, 1.0),  # image mixup (probability)
            "copy_paste": (True, 0.0, 1.0),
        }  # segment copy-paste (probability)

        # GA configs定义了遗传算法（Genetic Algorithm, GA）的配置参数，用于优化模型的超参数。
        pop_size = 50  # 种群大小
        mutation_rate_min = 0.01  # 变异率最小值
        mutation_rate_max = 0.5  # 变异率最大值
        crossover_rate_min = 0.5  # 交叉率最小值
        crossover_rate_max = 1  # 交叉率最大值
        min_elite_size = 2  # 最小精英个数
        max_elite_size = 5  # 最大精英个数
        tournament_size_min = 2  # 锦标赛选择的最小个数
        tournament_size_max = 10  # 锦标赛选择的最大个数

        with open(opt.hyp, errors="ignore") as f:
            hyp = yaml.safe_load(f)  # 加载超参数配置
            if "anchors" not in hyp:  # 设置键 "anchors" 的默认值为 3
                hyp["anchors"] = 3
        if opt.noautoanchor:
            # 如果 opt.noautoanchor 为 True，则从 hyp 和 meta 字典中删除 "anchors" 键。
            del hyp["anchors"], meta["anchors"]
        opt.noval, opt.nosave, save_dir = True, True, Path(
            opt.save_dir)  # 设置选项和保存目录
        # 定义进化文件路径
        evolve_yaml, evolve_csv = save_dir / "hyp_evolve.yaml", save_dir / "evolve.csv"
        # 从云存储下载文件
        if opt.bucket:
            # download evolve.csv if exists
            # "gsutil"：Google Cloud Storage 的命令行工具，用于与 GCS 进行交互。
            # "cp"：gsutil 的子命令，表示复制文件。
            # f"gs://{opt.bucket}/evolve.csv"：源文件路径，使用格式化字符串将 opt.bucket 的值插入到 GCS URL 中。
            # str(evolve_csv)：目标文件路径，将下载的文件保存到本地的 evolve_csv 路径。
            subprocess.run(
                [
                    "gsutil",
                    "cp",
                    f"gs://{opt.bucket}/evolve.csv",
                    str(evolve_csv),
                ]
            )

        # 删除 meta 字典中第一个值为 False 的项。
        del_ = [item for item, value_ in meta.items() if value_[0]
                is False]  # 筛选出需要删除的项
        hyp_GA = hyp.copy()  # 复制超参数字典
        for item in del_:
            del meta[item]  # 从 meta 字典中删除项
            del hyp_GA[item]  # 从 hyp_GA 字典中删除项

        # 定义了两个数组，用于存储搜索空间的边界
        # 这些边界是通过从 meta 字典中提取特定键的值来确定的。
        lower_limit = np.array([meta[k][1] for k in hyp_GA.keys()])
        upper_limit = np.array([meta[k][2] for k in hyp_GA.keys()])

        # 创建了一个名为 gene_ranges 的列表，用于存储每个基因在种群中的取值范围。
        # 这个列表的每个元素都是一个元组，包含了对应基因的下边界和上边界。
        gene_ranges = [(lower_limit[i], upper_limit[i])
                       for i in range(len(upper_limit))]

        # 种群的初始状态将被设置为特定的初始值或随机生成的值。
        initial_values = []

        # 处理了从先前的检查点恢复进化的情况
        # 如果选项 opt.resume_evolve 不为 None，则表示我们希望从一个先前保存的进化状态继续。
        if opt.resume_evolve is not None:
            assert os.path.isfile(
                ROOT / opt.resume_evolve), "evolve population path is wrong!"
            with open(ROOT / opt.resume_evolve, errors="ignore") as f:
                evolve_population = yaml.safe_load(f)
                for value in evolve_population.values():
                    value = np.array([value[k] for k in hyp_GA.keys()])
                    initial_values.append(list(value))

        # 处理了不从先前检查点恢复进化的情况
        # 如果选项 opt.resume_evolve 为 None，则代码将从指定目录中的 .yaml 文件生成初始值。
        else:
            yaml_files = [f for f in os.listdir(
                opt.evolve_population) if f.endswith(".yaml")]
            for file_name in yaml_files:
                with open(os.path.join(opt.evolve_population, file_name)) as yaml_file:
                    value = yaml.safe_load(yaml_file)
                    value = np.array([value[k] for k in hyp_GA.keys()])
                    initial_values.append(list(value))

        # 生成了种群的初始个体，这些个体的基因值在指定的搜索空间内随机生成。
        # 根据给定的基因范围和个体长度生成一个包含随机超参数的个体。
        if initial_values is None:
            population = [generate_individual(
                gene_ranges, len(hyp_GA)) for _ in range(pop_size)]
        elif pop_size > 1:
            population = [generate_individual(gene_ranges, len(
                hyp_GA)) for _ in range(pop_size - len(initial_values))]
            for initial_value in initial_values:
                population = [initial_value] + population

        # Run the genetic algorithm for a fixed number of generations
        # 实现了一个遗传算法，用于优化超参数
        # 首先，代码将 hyp_GA 的键转换为列表 list_keys，以便后续使用。
        list_keys = list(hyp_GA.keys())
        # 在一个固定的代数范围内（由 opt.evolve 指定），代码循环执行遗传算法的各个步骤。
        for generation in range(opt.evolve):
            # 在每一代中，如果代数大于等于1，代码会将当前种群的超参数保存到一个字典 save_dict 中，
            # 并将其写入到 evolve_population.yaml 文件中。
            if generation >= 1:
                save_dict = {}
                for i in range(len(population)):
                    little_dict = {list_keys[j]: float(
                        population[i][j]) for j in range(len(population[i]))}
                    save_dict[f"gen{str(generation)}number{str(i)}"] = little_dict

                with open(save_dir / "evolve_population.yaml", "w") as outfile:
                    yaml.dump(save_dict, outfile, default_flow_style=False)

            # 接下来，代码计算自适应精英大小 elite_size，该值随着代数的增加而变化。
            elite_size = min_elite_size + \
                int((max_elite_size - min_elite_size)
                    * (generation / opt.evolve))
            # 评估种群中每个个体的适应度。
            # 对于每个个体，代码将其超参数更新到 hyp_GA 中，并调用 train 函数进行训练，
            # 返回的结果用于计算适应度分数 fitness_scores。
            fitness_scores = []
            for individual in population:
                for key, value in zip(hyp_GA.keys(), individual):
                    hyp_GA[key] = value
                hyp.update(hyp_GA)
                results = train(hyp.copy(), opt, device, callbacks)
                callbacks = Callbacks()
                # Write mutation results
                keys = (
                    "metrics/precision",
                    "metrics/recall",
                    "metrics/mAP_0.5",
                    "metrics/mAP_0.5:0.95",
                    "val/box_loss",
                    "val/obj_loss",
                    "val/cls_loss",
                )
                # 训练完成后，代码会记录一些关键的训练结果，并将其打印出来。
                print_mutation(keys, results, hyp.copy(), save_dir, opt.bucket)
                fitness_scores.append(results[2])

            # 使用自适应锦标赛选择算法选择适应度最高的个体进行繁殖。
            selected_indices = []
            for _ in range(pop_size - elite_size):
                # 锦标赛大小 tournament_size 也是自适应的，随着代数的增加而变化。
                tournament_size = max(
                    max(2, tournament_size_min),
                    int(min(tournament_size_max, pop_size) -
                        (generation / (opt.evolve / 10))),
                )
                # 使用锦标赛选择来挑选最优个体
                tournament_indices = random.sample(
                    range(pop_size), tournament_size)
                tournament_fitness = [fitness_scores[j]
                                      for j in tournament_indices]
                winner_index = tournament_indices[tournament_fitness.index(
                    max(tournament_fitness))]
                selected_indices.append(winner_index)

            # 通过锦标赛选择，代码确定了用于繁殖的个体索引 selected_indices，并将精英个体添加到该列表中。
            elite_indices = [i for i in range(
                pop_size) if fitness_scores[i] in sorted(fitness_scores)[-elite_size:]]
            selected_indices.extend(elite_indices)
            # 在生成下一代时，代码通过交叉和变异操作创建新的个体。
            next_generation = []
            for _ in range(pop_size):
                parent1_index = selected_indices[random.randint(
                    0, pop_size - 1)]
                parent2_index = selected_indices[random.randint(
                    0, pop_size - 1)]
                # 交叉率crossover_rate是自适应的
                crossover_rate = max(
                    crossover_rate_min, min(
                        crossover_rate_max, crossover_rate_max - (generation / opt.evolve))
                )
                if random.uniform(0, 1) < crossover_rate:
                    crossover_point = random.randint(1, len(hyp_GA) - 1)
                    child = population[parent1_index][:crossover_point] + \
                        population[parent2_index][crossover_point:]
                else:
                    child = population[parent1_index]
                # 变异率 mutation_rate 也是自适应的
                mutation_rate = max(
                    mutation_rate_min, min(
                        mutation_rate_max, mutation_rate_max - (generation / opt.evolve))
                )
                for j in range(len(hyp_GA)):
                    if random.uniform(0, 1) < mutation_rate:
                        child[j] += random.uniform(-0.1, 0.1)
                        child[j] = min(
                            max(child[j], gene_ranges[j][0]), gene_ranges[j][1])
                next_generation.append(child)
            # 用新一代替换旧种群
            population = next_generation
        # 打印出找到的最佳解决方案
        best_index = fitness_scores.index(max(fitness_scores))
        best_individual = population[best_index]
        print("Best solution found:", best_individual)
        # 并绘制结果图表。
        plot_evolve(evolve_csv)
        # 日志信息记录了超参数进化的完成情况和结果保存的位置。
        LOGGER.info(
            f'Hyperparameter evolution finished {opt.evolve} generations\n'
            f"Results saved to {colorstr('bold', save_dir)}\n"
            f'Usage example: $ python train.py --hyp {evolve_yaml}'
        )


def generate_individual(input_ranges, individual_length):
    # 用于生成一个具有随机超参数的个体。
    # 该函数接受两个参数：input_ranges 和 individual_length。
    # input_ranges 是一个包含元组的列表，每个元组包含对应基因（超参数）的下限和上限。
    # individual_length 是个体中基因（超参数）的数量。
    # 函数的返回值是一个浮点数列表，表示生成的个体，其中每个基因值都在指定的范围内。
    """
    Generate an individual with random hyperparameters within specified ranges.

    Args:
        input_ranges (list[tuple[float, float]]): List of tuples where each tuple contains the lower and upper bounds
            for the corresponding gene (hyperparameter).
        individual_length (int): The number of genes (hyperparameters) in the individual.

    Returns:
        list[float]: A list representing a generated individual with random gene values within the specified ranges.

    Example:
        ```python
        input_ranges = [(0.01, 0.1), (0.1, 1.0), (0.9, 2.0)]
        individual_length = 3
        individual = generate_individual(input_ranges, individual_length)
        print(individual)  # Output: [0.035, 0.678, 1.456] (example output)
        ```

    Note:
        The individual returned will have a length equal to `individual_length`, with each gene value being a floating-point
        number within its specified range in `input_ranges`.
    """
    individual = []  # 用于存储生成的个体
    for i in range(individual_length):
        lower_bound, upper_bound = input_ranges[i]
        individual.append(random.uniform(
            lower_bound, upper_bound))  # 生成一个随机超参数
    return individual


def run(**kwargs):
    """
    Execute YOLOv5 training with specified options, allowing optional overrides through keyword arguments.

    Args:
        weights (str, optional): Path to initial weights. Defaults to ROOT / 'yolov5s.pt'.
        cfg (str, optional): Path to model YAML configuration. Defaults to an empty string.
        data (str, optional): Path to dataset YAML configuration. Defaults to ROOT / 'data/coco128.yaml'.
        hyp (str, optional): Path to hyperparameters YAML configuration. Defaults to ROOT / 'data/hyps/hyp.scratch-low.yaml'.
        epochs (int, optional): Total number of training epochs. Defaults to 100.
        batch_size (int, optional): Total batch size for all GPUs. Use -1 for automatic batch size determination. Defaults to 16.
        imgsz (int, optional): Image size (pixels) for training and validation. Defaults to 640.
        rect (bool, optional): Use rectangular training. Defaults to False.
        resume (bool | str, optional): Resume most recent training with an optional path. Defaults to False.
        nosave (bool, optional): Only save the final checkpoint. Defaults to False.
        noval (bool, optional): Only validate at the final epoch. Defaults to False.
        noautoanchor (bool, optional): Disable AutoAnchor. Defaults to False.
        noplots (bool, optional): Do not save plot files. Defaults to False.
        evolve (int, optional): Evolve hyperparameters for a specified number of generations. Use 300 if provided without a
            value.
        evolve_population (str, optional): Directory for loading population during evolution. Defaults to ROOT / 'data/ hyps'.
        resume_evolve (str, optional): Resume hyperparameter evolution from the last generation. Defaults to None.
        bucket (str, optional): gsutil bucket for saving checkpoints. Defaults to an empty string.
        cache (str, optional): Cache image data in 'ram' or 'disk'. Defaults to None.
        image_weights (bool, optional): Use weighted image selection for training. Defaults to False.
        device (str, optional): CUDA device identifier, e.g., '0', '0,1,2,3', or 'cpu'. Defaults to an empty string.
        multi_scale (bool, optional): Use multi-scale training, varying image size by ±50%. Defaults to False.
        single_cls (bool, optional): Train with multi-class data as single-class. Defaults to False.
        optimizer (str, optional): Optimizer type, choices are ['SGD', 'Adam', 'AdamW']. Defaults to 'SGD'.
        sync_bn (bool, optional): Use synchronized BatchNorm, only available in DDP mode. Defaults to False.
        workers (int, optional): Maximum dataloader workers per rank in DDP mode. Defaults to 8.
        project (str, optional): Directory for saving training runs. Defaults to ROOT / 'runs/train'.
        name (str, optional): Name for saving the training run. Defaults to 'exp'.
        exist_ok (bool, optional): Allow existing project/name without incrementing. Defaults to False.
        quad (bool, optional): Use quad dataloader. Defaults to False.
        cos_lr (bool, optional): Use cosine learning rate scheduler. Defaults to False.
        label_smoothing (float, optional): Label smoothing epsilon value. Defaults to 0.0.
        patience (int, optional): Patience for early stopping, measured in epochs without improvement. Defaults to 100.
        freeze (list, optional): Layers to freeze, e.g., backbone=10, first 3 layers = [0, 1, 2]. Defaults to [0].
        save_period (int, optional): Frequency in epochs to save checkpoints. Disabled if < 1. Defaults to -1.
        seed (int, optional): Global training random seed. Defaults to 0.
        local_rank (int, optional): Automatic DDP Multi-GPU argument. Do not modify. Defaults to -1.

    Returns:
        None: The function initiates YOLOv5 training or hyperparameter evolution based on the provided options.

    Examples:
        ```python
        import train
        train.run(data='coco128.yaml', imgsz=320, weights='yolov5m.pt')
        ```

    Notes:
        - Models: https://github.com/ultralytics/yolov5/tree/master/models
        - Datasets: https://github.com/ultralytics/yolov5/tree/master/data
        - Tutorial: https://docs.ultralytics.com/yolov5/tutorials/train_custom_data
    """
    opt = parse_opt(True)  # 解析命令行参数
    for k, v in kwargs.items():
        setattr(opt, k, v)  # 使用 setattr 函数将每个参数的值设置到 opt 对象中。
    main(opt)  # 调用 main 函数，开始训练或超参数进化的主要流程。
    return opt


# if __name__ == "__main__": 是一个常见的 Python 结构，
# 用于确保某些代码仅在脚本作为主程序运行时执行，而不是在它被作为模块导入时执行。
# 具体来说，当 Python 解释器运行一个脚本时，它会将特殊变量 __name__ 设为 "__main__"。
# 如果该脚本被导入到另一个脚本中，__name__ 的值将是该脚本的文件名，而不是 "__main__"。
if __name__ == "__main__":
    # parse_opt() 函数用来解析命令行参数，并返回一个包含这些参数的 argparse.Namespace 对象。
    # 通过解析命令行参数，用户可以在运行脚本时指定不同的选项和配置，从而灵活地控制脚本的行为。
    opt = parse_opt()
    main(opt)
