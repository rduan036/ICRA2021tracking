from __future__ import division
import time
import torch 
import torch.nn as nn
from torch.autograd import Variable
import numpy as np
import cv2 
from util import *
from tracking_units import *
# from tracking_units_backup import *
import argparse
import os 
import os.path as osp
from darknet import Darknet
import pickle as pkl
import pandas as pd
import random
import matplotlib.pyplot as plt
# import sys

# load yaml
task_info = TaskInfo()
batch_size = int(task_info.yolo_batch_size)
confidence = float(task_info.yolo_confidence)
nms_thesh = float(task_info.yolo_nms_thresh)
num_classes = int(task_info.yolo_num_classes)
classes = load_classes(task_info.yolo_classes_data)
videofile = task_info.video_path
target_class = task_info.tracking_object
layer_list = task_info.candidate_layer_list

# init yolo
colors = pkl.load(open("pallete", "rb"))
start_time = 0
CUDA = torch.cuda.is_available()
# Set up the neural network
print("Loading network.....")
model = Darknet(task_info.yolo_cfg_path)
model.load_weights(task_info.yolo_weight_path)
print("Network successfully loaded")
model.net_info["height"] = int(task_info.yolo_net_resolution)
inp_dim = int(model.net_info["height"])
assert inp_dim % 32 == 0
assert inp_dim > 32

#If there's a GPU availible, put the model on GPU
if CUDA:
    model.cuda()

#Set the model in evaluation mode
model.eval()

def write(x, results):
    c1 = tuple(x[1:3].int())
    c2 = tuple(x[3:5].int())
    img = results
    cls = int(x[-1])
    color = random.choice(colors)
    label = "{0}".format(classes[cls])
    cv2.rectangle(img, c1, c2, color, 1)
    t_size = cv2.getTextSize(label, cv2.FONT_HERSHEY_PLAIN, 1, 1)[0]
    c2 = c1[0] + t_size[0] + 3, c1[1] + t_size[1] + 4
    cv2.rectangle(img, c1, c2, color, -1)
    cv2.putText(img, label, (c1[0], c1[1] + t_size[1] + 4), cv2.FONT_HERSHEY_PLAIN, 1, [225, 255, 255], 1)
    return img

def task_manager(yolo_detection, target_class, select_rule = 'first_detected'):
    rect = [0, 0, 0, 0]
    task_activate = False
    for x in yolo_detection:
        cls = int(x[-1])
        label = "{0}".format(classes[cls])
        if select_rule == 'first_detected':
            if label == target_class:
                rect[0:2] = x[1:3].int().cpu().numpy()
                rect[2:4] = x[3:5].int().cpu().numpy()
                task_activate = True
                break
    return rect, task_activate

# ====================================================================================
# Load video data

cap = cv2.VideoCapture(videofile)

# ====================================================================================
# Live cam

#cap = cv2.VideoCapture(0)  for webcam

assert cap.isOpened(), 'Cannot capture source'

frames = 0  
task_activate = False
highest_layer = -1

plt.figure(num=1, figsize=(20, 20), dpi=80)

# tracking task config
# top_N_layer = 2
# top_N_features = 10
target_rect = [0, 0, 0, 0]
recom_idx_list = []
recom_score_list = []
recom_layers = []
multiscale_flag = True
target_feature = None
# spatiotemporal_buffer_size = 5
tracker = ORCFTracker(multiscale_flag)
# cv2.namedWindow('tracking')
# cv2.namedWindow('target feature')
inteval = 1

# start loading video
while cap.isOpened():
    ret, frame = cap.read()
    start = time.time()
    if ret:   
        img = prep_image(frame, inp_dim)
        im_dim = frame.shape[1], frame.shape[0]
        im_dim = torch.FloatTensor(im_dim).repeat(1, 2)
                     
        if CUDA:
            im_dim = im_dim.cuda()
            img = img.cuda()
        
        with torch.no_grad():
            output, layers_data = model(Variable(img), CUDA, highest_layer)

        # YOLO detection
        if not task_activate:

            output = write_results(output, confidence, num_classes, nms_conf = nms_thesh)
            if torch.is_tensor(output):
                im_dim = im_dim.repeat(output.size(0), 1)
                scaling_factor = torch.min(inp_dim/im_dim, 1)[0].view(-1, 1)

                output[:, [1, 3]] -= (inp_dim - scaling_factor*im_dim[:, 0].view(-1, 1))/2
                output[:, [2, 4]] -= (inp_dim - scaling_factor*im_dim[:, 1].view(-1, 1))/2

                output[:, 1:5] /= scaling_factor

                for i in range(output.shape[0]):
                    output[i, [1, 3]] = torch.clamp(output[i, [1, 3]], 0.0, im_dim[i, 0])
                    output[i, [2, 4]] = torch.clamp(output[i, [2, 4]], 0.0, im_dim[i, 1])


                target_rect, task_activate = task_manager(output, target_class)

                # if target is detected
                if task_activate:
                    # get recommendation score of candidate layers and feature maps
                    recom_idx_list, recom_score_list, layer_score, recom_layers = feature_recommender(layers_data,
                                                                                                      layer_list, frame,
                                                                                                      target_rect)
                    # rebuild target model from recommendated features
                    recom_heatmap_list = reconstruct_target_model(layers_data, layer_list, recom_idx_list, recom_score_list,
                                                            recom_layers)
                    recom_heatmap = 0
                    for heatmap in recom_heatmap_list:
                        recom_heatmap = recom_heatmap + cv2.resize(heatmap, (frame.shape[1], frame.shape[0]))
                    recom_heatmap = image_norm(recom_heatmap)
                    highest_layer = layer_list[max(recom_layers)]
                    # initial tracker
                    roi = target_rect.copy()
                    roi[2] = roi[2] - roi[0]
                    roi[3] = roi[3] - roi[1]
                    tracker.init(roi, frame.copy(), recom_heatmap)
                    cv2.rectangle(frame, (target_rect[0], target_rect[1]), (target_rect[2], target_rect[3]), (0, 255, 0), 1)
        else:
            recom_heatmap_list = reconstruct_target_model(layers_data, layer_list, recom_idx_list, recom_score_list,
                                                    recom_layers)
            recom_heatmap = 0
            for heatmap in recom_heatmap_list:
                recom_heatmap = recom_heatmap + cv2.resize(heatmap, (frame.shape[1], frame.shape[0]))
            recom_heatmap = image_norm(recom_heatmap)

            boundingbox, target_feature = tracker.update(frame.copy(), recom_heatmap)
            boundingbox = list(map(int, boundingbox))

            cv2.rectangle(frame, (boundingbox[0], boundingbox[1]),
                          (boundingbox[0] + boundingbox[2], boundingbox[1] + boundingbox[3]), (0, 255, 0), 2)

            FPS = int(1 / (time.time() - start))

            # get overall activation of recommended layer
            heatmap_overall = reconstruct_target_model(layers_data, layer_list, 0, 0, recom_layers)
            heatmap = heatmap_overall[-1]
            heatmap = cv2.resize(heatmap, (frame.shape[1], frame.shape[0]))

            plt.clf()
            plt.subplot(2, 2, 1)
            plt.title(f'overall activation of layer {layer_list[recom_layers[-1]]}')
            plt.imshow(heatmap, cmap='jet')
            plt.subplot(2, 2, 2)
            plt.title(f'recommended features of layer {layer_list[recom_layers[-1]]}')
            plt.imshow(recom_heatmap, cmap='jet')
            plt.subplot(2, 2, 3)
            plt.title('correlation filter searching region')
            plt.imshow(target_feature, cmap='jet')
            plt.subplot(2, 2, 4)
            plt.title(f'tracking result, FPS = {FPS}')
            plt.imshow(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
            plt.pause(0.001)

            save_name = './results/frame_' + str(frames) + '.jpg'
            plt.savefig(save_name)

        frames += 1

    else:
        break

plt.show()


