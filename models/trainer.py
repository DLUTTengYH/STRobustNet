import numpy as np
import matplotlib.pyplot as plt
import os

import utils

import torch
import torch.optim as optim

from misc.metric_tool import ConfuseMatrixMeter
from models.losses import cross_entropy, edge_loss, compute_smooth_loss, iou_loss

from misc.logger_tool import Logger, Timer
from models.STRobustNet import *
from utils import de_norm
import torchvision.utils as vutils
from tensorboardX import SummaryWriter

def make_iterative_func(func):
    def wrapper(vars):
        if isinstance(vars, list):
            return [wrapper(x) for x in vars]
        elif isinstance(vars, tuple):
            return tuple([wrapper(x) for x in vars])
        elif isinstance(vars, dict):
            return {k: wrapper(v) for k, v in vars.items()}
        else:
            return func(vars)

    return wrapper

@make_iterative_func
def tensor2float(vars):
    if isinstance(vars, float):
        return vars
    elif isinstance(vars, torch.Tensor):
        return vars.data.item()
    else:
        raise NotImplementedError("invalid input type for tensor2float")


@make_iterative_func
def tensor2numpy(vars):
    if isinstance(vars, np.ndarray):
        return vars
    elif isinstance(vars, torch.Tensor):
        return vars.data.cpu().numpy()
    else:
        raise NotImplementedError("invalid input type for tensor2numpy")


def save_scalars(logger, mode_tag, scalar_dict, global_step):
    scalar_dict = tensor2float(scalar_dict)
    for tag, values in scalar_dict.items():
        if not isinstance(values, list) and not isinstance(values, tuple):
            values = [values]
        for idx, value in enumerate(values):
            scalar_name = '{}/{}'.format(mode_tag, tag)
            # if len(values) > 1:
            scalar_name = scalar_name + "_" + str(idx)
            logger.add_scalar(scalar_name, value, global_step)


def save_images(logger, mode_tag, images_dict, global_step):
    images_dict = tensor2numpy(images_dict)
    for tag, values in images_dict.items():
        if not isinstance(values, list) and not isinstance(values, tuple):
            values = [values]
        for idx, value in enumerate(values):
            if len(value.shape) == 3:
                value = value[:, np.newaxis, :, :]
            value = value[:1]
            value = torch.from_numpy(value)

            image_name = '{}/{}'.format(mode_tag, tag)
            if len(values) > 1:
                image_name = image_name + "_" + str(idx)
            logger.add_image(image_name, vutils.make_grid(value, padding=0, nrow=1, normalize=True, scale_each=True),
                             global_step)



class CDTrainer():

    def __init__(self, args, dataloaders):
        
        self.args = args
        self.dataloaders = dataloaders

        self.n_class = args.n_class
        # define G
        self.net_G = define_G(args=args, gpu_ids=args.gpu_ids)

        self.device = torch.device("cuda:%s" % args.gpu_ids[0] if torch.cuda.is_available() and len(args.gpu_ids)>0
                                   else "cpu")
        print(self.device)

        # Learning rate and Beta1 for Adam optimizers
        self.lr = args.lr
        print("creating new summary file")
        self.tlogger = SummaryWriter(args.logdir)
        # define optimizers
        if args.optimizer == 'sgd':
            self.optimizer_G = optim.SGD(self.net_G.parameters(), lr=self.lr,
                                        momentum=0.9,
                                        weight_decay=5e-4)
        elif args.optimizer == 'Adam':
            self.optimizer_G = torch.optim.Adam(self.net_G.parameters(), lr=self.lr, 
                                        betas=(0.9, 0.99), eps=1e-10, weight_decay=5e-4)
        elif args.optimizer == 'AdamW':
            self.optimizer_G = optim.AdamW(self.net_G.parameters(), lr=self.lr, 
                                        betas=(0.9, 0.99), eps=1e-10, weight_decay=5e-4)
        self.args = args
        self.exp_lr_scheduler_G = get_scheduler(self.optimizer_G, args)

        self.running_metric = ConfuseMatrixMeter(n_class=2)

        # define logger file
        logger_path = os.path.join(args.checkpoint_dir, 'log.txt')
        self.logger = Logger(logger_path)
        self.logger.write_dict_str(args.__dict__)
        # define timer
        self.timer = Timer()
        self.batch_size = args.batch_size

        #  training log
        self.epoch_acc = 0
        self.best_val_acc = 0.0
        self.best_epoch_id = 0
        self.epoch_to_start = 0
        self.max_num_epochs = args.max_epochs

        self.global_step = 0
        self.steps_per_epoch = len(dataloaders['train'])
        self.total_steps = (self.max_num_epochs - self.epoch_to_start)*self.steps_per_epoch

        self.G_pred = None
        self.pred_vis = None
        self.batch = None
        self.G_loss = None

        self.token = None
        self.rate = 0

        self.is_training = False
        self.batch_id = 0
        self.epoch_id = 0
        self.checkpoint_dir = args.checkpoint_dir
        self.vis_dir = args.vis_dir
        self.scores = None

        # define the loss functions

        self.VAL_ACC = np.array([], np.float32)
        if os.path.exists(os.path.join(self.checkpoint_dir, 'val_acc.npy')):
            self.VAL_ACC = np.load(os.path.join(self.checkpoint_dir, 'val_acc.npy'))
        self.TRAIN_ACC = np.array([], np.float32)
        if os.path.exists(os.path.join(self.checkpoint_dir, 'train_acc.npy')):
            self.TRAIN_ACC = np.load(os.path.join(self.checkpoint_dir, 'train_acc.npy'))

        # check and create model dir
        if os.path.exists(self.checkpoint_dir) is False:
            os.mkdir(self.checkpoint_dir)
        if os.path.exists(self.vis_dir) is False:
            os.mkdir(self.vis_dir)


    def _load_checkpoint(self, ckpt_name='last_ckpt.pt'):

        if os.path.exists(os.path.join(self.checkpoint_dir, ckpt_name)):
            self.logger.write('loading last checkpoint...\n')
            # load the entire checkpoint
            checkpoint = torch.load(os.path.join(self.checkpoint_dir, ckpt_name),
                                    map_location=self.device)

            self.net_G.load_state_dict(checkpoint['model_G_state_dict'])

            self.optimizer_G.load_state_dict(checkpoint['optimizer_G_state_dict'])

            self.epoch_to_start = checkpoint['epoch_id'] + 1
            self.best_val_acc = checkpoint['best_val_acc']
            self.best_epoch_id = checkpoint['best_epoch_id']


            self.net_G.to(self.device)

            self.total_steps = (self.max_num_epochs - self.epoch_to_start)*self.steps_per_epoch

            self.logger.write('Epoch_to_start = %d, Historical_best_acc = %.4f (at epoch %d)\n' %
                  (self.epoch_to_start, self.best_val_acc, self.best_epoch_id))
            self.logger.write('\n')

        else:
            print('training from scratch...')

    def _timer_update(self):
        self.global_step = (self.epoch_id-self.epoch_to_start) * self.steps_per_epoch + self.batch_id

        self.timer.update_progress((self.global_step + 1) / self.total_steps)
        est = self.timer.estimated_remaining()
        imps = (self.global_step + 1) * self.batch_size / self.timer.get_stage_elapsed()
        return imps, est

    def _visualize_pred(self):
        if isinstance(self.G_pred, list):
            G_pred = torch.argmax(self.G_pred[-1], dim=1, keepdim=True)
        else:
            G_pred = torch.argmax(self.G_pred, dim=1, keepdim=True)
            # pred = torch.argmax(self.G_pred, dim=1, keepdim=True)
        # pred = torch.argmax(self.G_pred, dim=1, keepdim=True)
        pred_vis = G_pred * 255
        return pred_vis

    def _save_checkpoint(self, ckpt_name):
        torch.save({
            'epoch_id': self.epoch_id,
            'best_val_acc': self.best_val_acc,
            'best_epoch_id': self.best_epoch_id,
            'model_G_state_dict': self.net_G.state_dict(),
            'optimizer_G_state_dict': self.optimizer_G.state_dict(),
            'exp_lr_scheduler_G_state_dict': self.exp_lr_scheduler_G.state_dict(),
        }, os.path.join(self.checkpoint_dir, ckpt_name))

    def _update_lr_schedulers(self):
        self.exp_lr_scheduler_G.step()

    def _update_metric(self):
        """
        update metric
        """
        target = self.batch['L'].to(self.device).detach()
        if isinstance(self.G_pred, list):
            G_pred = torch.argmax(self.G_pred[-1], dim=1, keepdim=True)
        # elif len(self.G_pred.shape) == 3:
        #     # G_pred = torch.sigmoid()
        #     G_pred = torch.where(self.G_pred > 0.5, torch.ones_like(self.G_pred), torch.zeros_like(self.G_pred))
        else:
            G_pred = torch.argmax(self.G_pred, dim=1, keepdim=True)
        current_score = self.running_metric.update_cm(pr=G_pred.cpu().numpy(), gt=target.cpu().numpy())
        return current_score

    def _collect_running_batch_states(self):

        running_acc = self._update_metric()

        m = len(self.dataloaders['train'])
        if self.is_training is False:
            m = len(self.dataloaders['val'])

        imps, est = self._timer_update()
        if np.mod(self.batch_id, 50) == 0:
            if self.args.data_name == 'SYSU':
                message = 'Is_training: %s. [%d,%d][%d,%d], imps: %.2f, est: %.2fh, G_loss: %.5f, ce_loss: %.5f, iou_loss: %.5f, running_mf1: %.5f\n' %\
                        (self.is_training, self.epoch_id, self.max_num_epochs-1, self.batch_id, m,
                        imps*self.batch_size, est,
                        self.G_loss.item(), self.ce_loss.item(), self.iou_loss.item(), running_acc)
                self.logger.write(message)
            elif self.args.net_G == 'STR3':
                message = 'Is_training: %s. [%d,%d][%d,%d], imps: %.2f, est: %.2fh, G_loss: %.5f, edge_loss: %.5f, smooth_loss: %.5f, iou_loss: %.5f, unc_loss: %.5f, running_mf1: %.5f\n' %\
                        (self.is_training, self.epoch_id, self.max_num_epochs-1, self.batch_id, m,
                        imps*self.batch_size, est,
                        self.G_loss.item(), self.edge_loss.item(), self.smooth_loss.item(), self.iou_loss.item(), self.unc_loss.item(), running_acc)
                self.logger.write(message)
            else:
                # message = 'Is_training: %s. [%d,%d][%d,%d], imps: %.2f, est: %.2fh, G_loss: %.5f, edge_loss: %.5f, smooth_loss: %.5f, iou_loss: %.5f, running_mf1: %.5f\n' %\
                #         (self.is_training, self.epoch_id, self.max_num_epochs-1, self.batch_id, m,
                #         imps*self.batch_size, est,
                #         self.G_loss.item(), self.edge_loss.item(), self.smooth_loss.item(), self.iou_loss.item(), running_acc)
                # self.logger.write(message)
                message = 'Is_training: %s. [%d,%d][%d,%d], imps: %.2f, est: %.2fh, G_loss: %.5f, ce_loss: %.5f, smooth_loss: %.5f, iou_loss:%.5f, running_mf1: %.5f\n' %\
                        (self.is_training, self.epoch_id, self.max_num_epochs-1, self.batch_id, m,
                        imps*self.batch_size, est,
                        self.G_loss.item(), self.ce_loss.item(), self.smooth_loss.item(), self.iou_loss.item(), running_acc)
                self.logger.write(message)



    def _collect_epoch_states(self):
        scores = self.running_metric.get_scores()
        self.scores = scores
        self.epoch_acc = scores['mf1']
        self.logger.write('Is_training: %s. Epoch %d / %d, epoch_mF1= %.5f\n' %
              (self.is_training, self.epoch_id, self.max_num_epochs-1, self.epoch_acc))
        message = ''
        for k, v in scores.items():
            message += '%s: %.5f ' % (k, v)
        self.logger.write(message+'\n')
        self.logger.write('\n')

    def _update_checkpoints(self):

        # save current model
        self._save_checkpoint(ckpt_name='last_ckpt.pt')
        self.logger.write('Lastest model updated. Epoch_acc=%.4f, Historical_best_acc=%.4f (at epoch %d)\n'
              % (self.epoch_acc, self.best_val_acc, self.best_epoch_id))
        self.logger.write('\n')

        # update the best model (based on eval acc)
        if self.epoch_acc > self.best_val_acc:
            self.best_val_acc = self.epoch_acc
            self.best_epoch_id = self.epoch_id
            self._save_checkpoint(ckpt_name='best_ckpt.pt')
            self.logger.write('*' * 10 + 'Best model updated!\n')
            self.logger.write('\n')

    def _update_training_acc_curve(self):
        # update train acc curve
        self.TRAIN_ACC = np.append(self.TRAIN_ACC, [self.epoch_acc])
        np.save(os.path.join(self.checkpoint_dir, 'train_acc.npy'), self.TRAIN_ACC)

    def _update_val_acc_curve(self):
        # update val acc curve
        self.VAL_ACC = np.append(self.VAL_ACC, [self.epoch_acc])
        np.save(os.path.join(self.checkpoint_dir, 'val_acc.npy'), self.VAL_ACC)

    def _clear_cache(self):
        self.running_metric.clear()


    def _forward_pass(self, batch):
        self.batch = batch
        img_in1 = batch['A'].to(self.device)
        img_in2 = batch['B'].to(self.device)
        if self.args.net_G == 'STR3':
            gt = batch['L'].to(self.device)
            self.G_pred, self.uncertainty, self.gt_mask = self.net_G(img_in1, img_in2, gt)
        else:
            self.G_pred = self.net_G(img_in1, img_in2)
       


    def _backward_G(self):
        gt = self.batch['L'].to(self.device).long().squeeze(1)
        if self.args.data_name == "SYSU":
            self.ce_loss = cross_entropy(self.G_pred, gt)
            # self.edge_loss = edge_loss(self.G_pred, gt)
            # self.dice_loss = dice_loss(self.G_pred, gt)
            self.iou_loss = iou_loss(self.G_pred, gt)
            self.smooth_loss = compute_smooth_loss(gt, self.G_pred[:, 1, :, :])
            self.G_loss = self.ce_loss + self.smooth_loss + 0.7 * self.iou_loss
        else:
            # self.G_loss = edge_loss(self.G_pred, gt)
            self.edge_loss = edge_loss(self.G_pred, gt)
            self.smooth_loss = compute_smooth_loss(gt, self.G_pred[:, 1, :, :])
            self.iou_loss = iou_loss(self.G_pred, gt)
            # self.G_loss = self.edge_loss + self.smooth_loss + 0.5 * self.iou_loss
            # self.ce_loss = cross_entropy(self.G_pred, gt)
            self.G_loss = self.edge_loss + self.smooth_loss + 0.5 * self.iou_loss

        self.G_loss.backward()


    def train_models(self):

        self._load_checkpoint()

        # loop over the dataset multiple times
        for self.epoch_id in range(self.epoch_to_start, self.max_num_epochs):
            print("rate: ", self.rate)
            ################## train #################
            ##########################################
            self._clear_cache()
            self.is_training = True
             # Set model to training mode
            self.net_G.train()
            # self.net_G.train()
            # Iterate over data.
            self.logger.write('lr: %0.7f\n' % self.optimizer_G.param_groups[0]['lr'])
            for self.batch_id, batch in enumerate(self.dataloaders['train'], 0):
                self._forward_pass(batch)
                # update G
                self.optimizer_G.zero_grad()
                self._backward_G()
                self.optimizer_G.step()
                self._collect_running_batch_states()
                self._timer_update()

            self._collect_epoch_states()
            self._update_training_acc_curve()
            self._update_lr_schedulers()


            ################## Eval ##################
            ##########################################
            self.logger.write('Begin evaluation...\n')
            self._clear_cache()
            self.is_training = False
            # else:
            self.net_G.eval()

                # Iterate over data.
            for self.batch_id, batch in enumerate(self.dataloaders['val'], 0):
                with torch.no_grad():
                    self._forward_pass(batch)
                self._collect_running_batch_states()

            self._collect_epoch_states()

        ########### Update_Checkpoints ###########
        ##########################################
            self._update_val_acc_curve()
            self._update_checkpoints()

