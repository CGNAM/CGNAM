# --------------------------------------------------------
# Tensorflow Faster R-CNN
# Licensed under The MIT License [see LICENSE for details]
# Written by Jiasen Lu, Jianwei Yang, based on code from Ross Girshick
# --------------------------------------------------------
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import _init_paths
import os
import sys
import numpy as np
import argparse
import pprint
import pdb
import time
import cv2
import torch
from torch.autograd import Variable
import torch.nn as nn
import torch.optim as optim

import torchvision.transforms as transforms
import torchvision.datasets as dset
from scipy.misc import imread
from roi_data_layer.roidb import combined_roidb
from roi_data_layer.roibatchLoader import roibatchLoader
from model.utils.config import cfg, cfg_from_file, cfg_from_list, get_output_dir
from model.rpn.bbox_transform import clip_boxes
from model.nms.nms_wrapper import nms
from model.rpn.bbox_transform import bbox_transform_inv
from model.utils.net_utils import save_net, load_net, vis_detections
from model.utils.blob import im_list_to_blob
from model.faster_rcnn.vgg16 import vgg16
from model.faster_rcnn.resnet import resnet
import pdb

try:
    xrange          # Python 2
except NameError:
    xrange = range  # Python 3


def parse_args(arg):
  """
  Parse input arguments
  """
  parser = argparse.ArgumentParser(description='Train a Fast R-CNN network')
  parser.add_argument('--dataset', dest='dataset',
                      help='training dataset',
                      default='pascal_voc', type=str)
  parser.add_argument('--cfg', dest='cfg_file',
                      help='optional config file',
                      default='cfgs/res101.yml', type=str)
  parser.add_argument('--net', dest='net',
                      help='vgg16, res50, res101, res152',
                      default='res101', type=str)
  parser.add_argument('--set', dest='set_cfgs',
                      help='set config keys', default=None,
                      nargs=argparse.REMAINDER)
  parser.add_argument('--load_dir', dest='load_dir',
                      help='directory to load models',
                      default="models")
  # parser.add_argument('--image_dir', dest='image_dir',
  #                     help='directory to load images for demo',
  #                     default="images")
  parser.add_argument('--cuda', dest='cuda',
                      help='whether use CUDA',
                      default = True,
                      action='store_true')
  parser.add_argument('--mGPUs', dest='mGPUs',
                      help='whether use multiple GPUs',
                      action='store_true')
  parser.add_argument('--cag', dest='class_agnostic',
                      help='whether perform class_agnostic bbox regression',
                      action='store_true')
  parser.add_argument('--parallel_type', dest='parallel_type',
                      help='which part of model to parallel, 0: all, 1: model before roi pooling',
                      default=0, type=int)
  parser.add_argument('--checksession', dest='checksession',
                      help='checksession to load model',
                      default=1, type=int)
  parser.add_argument('--checkepoch', dest='checkepoch',
                      help='checkepoch to load network',
                      default=6, type=int)
  parser.add_argument('--checkpoint', dest='checkpoint',
                      help='checkpoint to load network',
                      default=18197, type=int)
                      # default=39937, type=int)
  parser.add_argument('--bs', dest='batch_size',
                      help='batch_size',
                      default=1, type=int)
  parser.add_argument('--vis', dest='vis',
                      help='visualization mode',
                      action='store_true')
  args = parser.parse_args(arg)
  return args

lr = cfg.TRAIN.LEARNING_RATE
momentum = cfg.TRAIN.MOMENTUM
weight_decay = cfg.TRAIN.WEIGHT_DECAY

def _get_image_blob(im):
  """Converts an image into a network input.
  Arguments:
    im (ndarray): a color image in BGR order
  Returns:
    blob (ndarray): a data blob holding an image pyramid
    im_scale_factors (list): list of image scales (relative to im) used
      in the image pyramid
  """
  im_orig = im.astype(np.float32, copy=True)
  im_orig -= cfg.PIXEL_MEANS

  im_shape = im_orig.shape
  im_size_min = np.min(im_shape[0:2])
  im_size_max = np.max(im_shape[0:2])

  processed_ims = []
  im_scale_factors = []

  for target_size in cfg.TEST.SCALES:
    im_scale = float(target_size) / float(im_size_min)
    # Prevent the biggest axis from being more than MAX_SIZE
    if np.round(im_scale * im_size_max) > cfg.TEST.MAX_SIZE:
      im_scale = float(cfg.TEST.MAX_SIZE) / float(im_size_max)
    im = cv2.resize(im_orig, None, None, fx=im_scale, fy=im_scale,
            interpolation=cv2.INTER_LINEAR)
    im_scale_factors.append(im_scale)
    processed_ims.append(im)

  # Create a blob to hold the input images
  blob = im_list_to_blob(processed_ims)

  return blob, np.array(im_scale_factors)

class Model(object):
  def __init__(self, args):
    print('Called with args:')
    print(args)

    if args.cfg_file is not None:
      cfg_from_file(args.cfg_file)
    if args.set_cfgs is not None:
      cfg_from_list(args.set_cfgs)

    cfg.CUDA = args.cuda and torch.cuda.is_available()
    cfg.class_agnostic = args.class_agnostic
    cfg.vis = args.vis

    print('Using config:')
    pprint.pprint(cfg)
    np.random.seed(cfg.RNG_SEED)

    # train set
    # -- Note: Use validation set and disable the flipped to enable faster loading.

    input_dir = args.load_dir + "/" + args.net + "/" + args.dataset
    if not os.path.exists(input_dir):
      raise Exception('There is no input directory for loading network from ' + input_dir)
    load_name = os.path.join(input_dir,
      'faster_rcnn_{}_{}_{}.pth'.format(args.checksession, args.checkepoch, args.checkpoint))

    pascal_classes = np.asarray(['__background__',
                        'person', 'bike', 'motorbike', 'car', 'bus'])
    # pascal_classes = np.asarray(['__background__',
    #                     'object'])
    cfg.classes = pascal_classes

    # initilize the network here.
    if args.net == 'vgg16':
      fasterRCNN = vgg16(pascal_classes, pretrained=False, class_agnostic=args.class_agnostic)
    elif args.net == 'res101':
      fasterRCNN = resnet(pascal_classes, 101, pretrained=False, class_agnostic=args.class_agnostic)
    elif args.net == 'res50':
      fasterRCNN = resnet(pascal_classes, 50, pretrained=False, class_agnostic=args.class_agnostic)
    elif args.net == 'res152':
      fasterRCNN = resnet(pascal_classes, 152, pretrained=False, class_agnostic=args.class_agnostic)
    else:
      print("network is not defined")
      pdb.set_trace()

    fasterRCNN.create_architecture()

    print("load checkpoint %s" % (load_name))
    if args.cuda > 0:
      checkpoint = torch.load(load_name)
    else:
      checkpoint = torch.load(load_name, map_location=(lambda storage, loc: storage))
    fasterRCNN.load_state_dict(checkpoint['model'])
    if 'pooling_mode' in checkpoint.keys():
      cfg.POOLING_MODE = checkpoint['pooling_mode']
    
    if args.cuda > 0:
      fasterRCNN.cuda()
    fasterRCNN.eval()
    
    self.model = fasterRCNN

    print('load model successfully!')

    # pdb.set_trace()

    print("load checkpoint %s" % (load_name))

  def run(self, ori_im, det_output, use_nms = True):
    # initilize the tensor holder here.
    im_data = torch.FloatTensor(1)
    im_info = torch.FloatTensor(1)
    num_boxes = torch.LongTensor(1)
    gt_boxes = torch.FloatTensor(1)

    # ship to cuda
    if cfg.CUDA:
      im_data = im_data.cuda()
      im_info = im_info.cuda()
      num_boxes = num_boxes.cuda()
      gt_boxes = gt_boxes.cuda()

    # make variable
    with torch.no_grad():
      im_data = Variable(im_data)
      im_info = Variable(im_info)
      num_boxes = Variable(num_boxes)
      gt_boxes = Variable(gt_boxes)

    start = time.time()
    max_per_image = 100
    thresh = 0.05
    vis = cfg.vis


    total_tic = time.time()
    # BGR image
    im = np.array(ori_im)

    blobs, im_scales = _get_image_blob(im)
    assert len(im_scales) == 1, "Only single-image batch implemented"
    im_blob = blobs
    im_info_np = np.array([[im_blob.shape[1], im_blob.shape[2], im_scales[0]]], dtype=np.float32)

    im_data_pt = torch.from_numpy(im_blob)
    im_data_pt = im_data_pt.permute(0, 3, 1, 2)
    im_info_pt = torch.from_numpy(im_info_np)

    im_data.data.resize_(im_data_pt.size()).copy_(im_data_pt)
    im_info.data.resize_(im_info_pt.size()).copy_(im_info_pt)
    gt_boxes.data.resize_(1, 1, 5).zero_()
    num_boxes.data.resize_(1).zero_()

    # pdb.set_trace()
    det_tic = time.time()

    rois, cls_prob, bbox_pred, \
    rpn_loss_cls, rpn_loss_box, \
    RCNN_loss_cls, RCNN_loss_bbox, \
    rois_label, object_features, frame_feature = self.model(im_data, im_info, gt_boxes, num_boxes, ret_feat = 'pool5')

    scores = cls_prob.data
    boxes = rois.data[:, :, 1:5]

    if cfg.TEST.BBOX_REG:
        # Apply bounding-box regression deltas
        box_deltas = bbox_pred.data
        if cfg.TRAIN.BBOX_NORMALIZE_TARGETS_PRECOMPUTED:
        # Optionally normalize targets by a precomputed mean and stdev
          if cfg.class_agnostic:
              if cfg.CUDA:
                  box_deltas = box_deltas.view(-1, 4) * torch.FloatTensor(cfg.TRAIN.BBOX_NORMALIZE_STDS).cuda() \
                            + torch.FloatTensor(cfg.TRAIN.BBOX_NORMALIZE_MEANS).cuda()
              else:
                  box_deltas = box_deltas.view(-1, 4) * torch.FloatTensor(cfg.TRAIN.BBOX_NORMALIZE_STDS) \
                            + torch.FloatTensor(cfg.TRAIN.BBOX_NORMALIZE_MEANS)

              box_deltas = box_deltas.view(1, -1, 4)
          else:
              if cfg.CUDA:
                  box_deltas = box_deltas.view(-1, 4) * torch.FloatTensor(cfg.TRAIN.BBOX_NORMALIZE_STDS).cuda() \
                            + torch.FloatTensor(cfg.TRAIN.BBOX_NORMALIZE_MEANS).cuda()
              else:
                  box_deltas = box_deltas.view(-1, 4) * torch.FloatTensor(cfg.TRAIN.BBOX_NORMALIZE_STDS) \
                            + torch.FloatTensor(cfg.TRAIN.BBOX_NORMALIZE_MEANS)
              box_deltas = box_deltas.view(1, -1, 4 * len(cfg.classes))

        pred_boxes = bbox_transform_inv(boxes, box_deltas, 1)
        pred_boxes = clip_boxes(pred_boxes, im_info.data, 1)
    else:
        # Simply repeat the boxes, once for each class
        _ = torch.from_numpy(np.tile(boxes, (1, scores.shape[1])))
        pred_boxes = _.cuda() if cfg.CUDA else _

    pred_boxes /= im_scales[0]
    scores = scores.squeeze()
    pred_boxes = pred_boxes.squeeze()
    
    object_features = object_features.data.squeeze()
    res_feat = frame_feature.data.squeeze(0)
    res_dect = torch.zeros((1,6)).cuda() if cfg.CUDA else torch.zeros((1,6))

    det_toc = time.time()
    detect_time = det_toc - det_tic
    misc_tic = time.time()

    if vis:
      im2show = np.copy(im)
    if use_nms:
      for j in xrange(1, len(cfg.classes)):
        inds = torch.nonzero(scores[:,j]>thresh).view(-1)
        # if there is det
        if inds.numel() > 0:
          cls_scores = scores[:,j][inds]
          if cfg.class_agnostic:
            cls_boxes = pred_boxes[inds, :]
          else:
            cls_boxes = pred_boxes[inds][:, j * 4:(j + 1) * 4]
          
          cls_dets = torch.cat((cls_boxes, cls_scores.unsqueeze(1)), 1)
          _, order = torch.sort(cls_scores, 0, True)
          cls_dets = cls_dets[order]

          keep = nms(cls_dets, cfg.TEST.NMS, force_cpu=not cfg.USE_GPU_NMS)
          cls_dets = cls_dets[keep.view(-1).long()]
          obj_feat = object_features[inds,:][order][keep.view(-1).long()]

          if vis:
            im2show = vis_detections(im2show, cfg.classes[j], cls_dets.cpu().numpy(), thresh)
            
          cls_ids = torch.ones((cls_dets.size(0),1)).fill_(j-1)
          if cfg.CUDA:
            cls_ids = cls_ids.cuda()
          cls_dets = torch.cat((cls_dets, cls_ids),1)

          res_dect = torch.cat((res_dect, cls_dets), 0)
          res_feat = torch.cat((res_feat, obj_feat), 0)
    else:
      # assert vis==False
      # no bg
      # cls_conf, cls_id = torch.max(scores[:,1:],1)
      # bg
      cls_conf, cls_id = torch.max(scores,1)
      if cfg.class_agnostic:
        cls_boxes = pred_boxes
      else:
        # no bg
        # cls_boxes = [box[(i+1)*4:(i+2)*4] for box,i in zip(pred_boxes,cls_id)]
        # with bg
        cls_boxes = [box[(i)*4:(i+1)*4] for box,i in zip(pred_boxes,cls_id)]
        cls_boxes = torch.stack(cls_boxes,dim=0)
      cls_dets = torch.cat((cls_boxes, cls_conf.unsqueeze(1)), dim=1)
      if vis:
        im2show = vis_detections(im2show, cfg.classes[0], cls_dets.cpu().numpy(), thresh)
      cls_dets = torch.cat((cls_dets,cls_id.unsqueeze(1).float()),dim=1)
      res_dect = torch.cat((res_dect, cls_dets),dim=0)
      res_feat = torch.cat((res_feat, object_features),dim=0)


    # print("total", res_dect.size(0),"dets")
    
    if vis:
        cv2.imshow("frame", im2show)
        total_toc = time.time()
        total_time = total_toc - total_tic
        frame_rate = 1 / total_time
        print('Frame rate:', frame_rate)
        cv2.waitKey(1)
        if det_output:
          det_output.write(im2show)
    return res_feat.cpu().numpy(), res_dect.cpu().numpy()

class FasterRCNN(object):
    def __init__(self, arg=None):
        args = parse_args(arg)
        self.model = Model(args)
        self.class_names = cfg.classes
        
    def __call__(self, ori_img, det_output, use_nms=True):
        return self.model.run(ori_img, det_output, use_nms)