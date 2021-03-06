from __future__ import division

from models import *
from utils.logger import *
from utils.utils import *
from utils.datasets import *
from utils.parse_config import *
import test

from terminaltables import AsciiTable

import os
import sys
import time
import datetime
import argparse
import numpy as np
import Image
import GenerateYOLOData

import torch
from torch.utils.data import DataLoader
from torchvision import datasets
from torchvision import transforms
from torch.autograd import Variable
import torch.optim as optim

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--epochs", type=int, default=100, help="number of epochs")
    parser.add_argument("--batch_size", type=int, default=8, help="size of each image batch")
    parser.add_argument("--gradient_accumulations", type=int, default=2, help="number of gradient accums before step")
    parser.add_argument("--model_def", type=str, default="config/yolov3.cfg", help="path to model definition file")
    parser.add_argument("--data_config", type=str, default="config/coco.data", help="path to data config file")
    parser.add_argument("--pretrained_weights", type=str, help="if specified starts from checkpoint model")
    parser.add_argument("--n_cpu", type=int, default=8, help="number of cpu threads to use during batch generation")
    parser.add_argument("--img_size", type=int, default=416, help="size of each image dimension")
    parser.add_argument("--checkpoint_interval", type=int, default=1, help="interval between saving model weights")
    parser.add_argument("--evaluation_interval", type=int, default=1, help="interval evaluations on validation set")
    parser.add_argument("--compute_map", default=False, help="if True computes mAP every tenth batch")
    parser.add_argument("--multiscale_training", default=True, help="allow for multi-scale training")
    opt = parser.parse_args()
    print(opt)

    logger = Logger("logs")

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

    os.makedirs("output", exist_ok=True)
    os.makedirs("checkpoints", exist_ok=True)

    # Get data configuration
    data_config = parse_data_config(opt.data_config)
    train_path = data_config["train"]
    valid_path = data_config["valid"]
    class_names = load_classes(data_config["names"])

    # Initiate model
    model = Darknet(opt.model_def).to(device)
    model.apply(weights_init_normal)

    # If specified we start from checkpoint
    if opt.pretrained_weights:
        if opt.pretrained_weights.endswith(".pth"):
            model.load_state_dict(torch.load(opt.pretrained_weights))
        else:
            model.load_darknet_weights(opt.pretrained_weights)



    optimizer = torch.optim.Adam(model.parameters())

    metrics = [
        "grid_size",
        "loss",
        "x",
        "y",
        "w",
        "h",
        "conf",
        "cls",
        "cls_acc",
        "recall50",
        "recall75",
        "precision",
        "conf_obj",
        "conf_noobj",
    ]

    best_map = 0
    better_model_found = False

    for epoch in range(opt.epochs):
        model.train()
        start_time = time.time()
        #Generate data 
        print_labels=False
        batchSize = 1
        Int = lambda: 1.05e-3*(0.08+0.8*np.random.rand())
        Ds = lambda: 0.10*np.sqrt((0.05 + 1.0*np.random.rand()))
        st = lambda: 0.04 + 0.01*np.random.rand()
        
    
        length = 512
        times = 128
        
        dA = lambda: 0.00006 * (0.7 + np.random.rand())
        dX = lambda: 0.00006* (0.7 + np.random.rand())
        bgnoiselev = lambda: 0.0006* (0.7 + np.random.rand())
    
        time_reduction = 16
        length_reduction = 64
        downsampling_factor_for_length = 1
        nump=2 #number of particles
        data_generator = GenerateYOLOData.Generate8b8batchboxesv2Generator(bgnoiselev=bgnoiselev,
                                                         Int=Int,
                                                         st=st,
                                                         Ds=Ds, 
                                                         dA=dA,
                                                         dX=dX,
                                                         batchsize=batchSize,
                                                         length=length,
                                                         times=times,
                                                         bgexp=None,
                                                         print_labels=print_labels,
                                                         time_reduction=time_reduction,
                                                         length_reduction=length_reduction,
                                                         downsampling_factor_for_length=downsampling_factor_for_length,
                                                         nump=nump)
        val,valL,v1 = next(data_generator)
        
        val=val[...,:1]
        valld = valL[0]
        
        YOLOLabels=GenerateYOLOData.ConvertTrajToBoundingBoxes(v1,batchSize,nump,length=512,times=128,treshold=0.5)
        v1 = np.sum(v1[0,...],0).T
        # Get dataloader
        dataset = ListDataset(train_path, augment=False, multiscale=opt.multiscale_training)
        dataloader = torch.utils.data.DataLoader(
        dataset,
        batch_size=opt.batch_size,
        shuffle=True,
        num_workers=opt.n_cpu,
        pin_memory=True,
        collate_fn=dataset.collate_fn,
    )
        for batch_i, (_, imgs, targets) in enumerate(dataloader):
            
            
            
            batches_done = len(dataloader) * epoch + batch_i #!!

            imgs = Variable(v1.to(device))
            targets = Variable(YOLOLabels.to(device), requires_grad=False)

            loss, outputs = model(imgs, targets)
            loss.backward()

            if batches_done % opt.gradient_accumulations:
                # Accumulates gradient before each step
                optimizer.step()
                optimizer.zero_grad()

            # ----------------
            #   Log progress
            # ----------------

            log_str = "\n---- [Epoch %d/%d, Batch %d/%d] ----\n" % (epoch, opt.epochs, batch_i, len(dataloader))

            metric_table = [["Metrics", *[f"YOLO Layer {i}" for i in range(len(model.yolo_layers))]]]

            # Log metrics at each YOLO layer
            for i, metric in enumerate(metrics):
                formats = {m: "%.6f" for m in metrics}
                formats["grid_size"] = "%2d"
                formats["cls_acc"] = "%.2f%%"
                row_metrics = [formats[metric] % yolo.metrics.get(metric, 0) for yolo in model.yolo_layers]
                metric_table += [[metric, *row_metrics]]

                # Tensorboard logging
                tensorboard_log = []
                for j, yolo in enumerate(model.yolo_layers):
                    for name, metric in yolo.metrics.items():
                        if name != "grid_size":
                            tensorboard_log += [(f"{name}_{j+1}", metric)]
                tensorboard_log += [("loss", loss.item())]
                logger.list_of_scalars_summary(tensorboard_log, batches_done)

            log_str += AsciiTable(metric_table).table
            log_str += f"\nTotal loss {loss.item()}"

            # Determine approximate time left for epoch
            epoch_batches_left = len(dataloader) - (batch_i + 1)
            time_left = datetime.timedelta(seconds=epoch_batches_left * (time.time() - start_time) / (batch_i + 1))
            log_str += f"\n---- ETA {time_left}"

            print(log_str)

            model.seen += imgs.size(0)

        if epoch % opt.evaluation_interval == 0:
            print("\n---- Evaluating Model ----")
            # Evaluate the model on the validation set
            precision, recall, AP, f1, ap_class = test.evaluate(
                model,
                path=valid_path,
                iou_thres=0.5,
                conf_thres=0.5,
                nms_thres=0.5,
                img_size=opt.img_size,
                batch_size=8,
            )
            evaluation_metrics = [
                ("val_precision", precision.mean()),
                ("val_recall", recall.mean()),
                ("val_mAP", AP.mean()),
                ("val_f1", f1.mean()),
            ]
            logger.list_of_scalars_summary(evaluation_metrics, epoch)

            # Print class APs and mAP
            ap_table = [["Index", "Class name", "AP"]]
            for i, c in enumerate(ap_class):
                ap_table += [[c, class_names[c], "%.5f" % AP[i]]]
            print(AsciiTable(ap_table).table)
            print(f"---- mAP {AP.mean()}")
            if AP.mean() > best_map:
                best_map = AP.mean()
                better_model_found = True

        if epoch % opt.checkpoint_interval == 0:
            if better_model_found:
                torch.save(model.state_dict(), f"checkpoints/yolov3_ckpt_%d.pth" % epoch)
                better_model_found = False
