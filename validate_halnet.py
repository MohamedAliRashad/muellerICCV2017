import torch
from torch.autograd import Variable

import converter
import synthhands_handler
import trainer
import validator
import time
from magic import display_est_time_loop
import losses as my_losses
from debugger import print_verbose
from HALNet import HALNet
import visualize
import converter as conv

DEBUG_VISUALLY = False

def validate(valid_loader, model, optimizer, valid_vars, control_vars, verbose=True):
    curr_epoch_iter = 1
    for batch_idx, (data, target) in enumerate(valid_loader):
        control_vars['batch_idx'] = batch_idx
        if batch_idx < control_vars['iter_size']:
            print_verbose("\rPerforming first iteration; current mini-batch: " +
                          str(batch_idx + 1) + "/" + str(control_vars['iter_size']), verbose, n_tabs=0, erase_line=True)
        # start time counter
        start = time.time()
        # get data and targetas cuda variables
        target_heatmaps, target_joints, target_joints_z = target
        data, target_heatmaps = Variable(data), Variable(target_heatmaps)
        if valid_vars['use_cuda']:
            data = data.cuda()
            target_heatmaps = target_heatmaps.cuda()
        # visualize if debugging
        # get model output
        output = model(data)
        # accumulate loss for sub-mini-batch
        if valid_vars['cross_entropy']:
            loss_func = my_losses.cross_entropy_loss_p_logq
        else:
            loss_func = my_losses.euclidean_loss
        loss = my_losses.calculate_loss_HALNet(loss_func,
            output, target_heatmaps, model.joint_ixs, model.WEIGHT_LOSS_INTERMED1,
            model.WEIGHT_LOSS_INTERMED2, model.WEIGHT_LOSS_INTERMED3,
            model.WEIGHT_LOSS_MAIN, control_vars['iter_size'])

        if DEBUG_VISUALLY:
            for i in range(control_vars['max_mem_batch']):
                filenamebase_idx = (batch_idx * control_vars['max_mem_batch']) + i
                filenamebase = valid_loader.dataset.get_filenamebase(filenamebase_idx)
                fig = visualize.create_fig()
                #visualize.plot_joints_from_heatmaps(output[3][i].data.numpy(), fig=fig,
                #                                    title=filenamebase, data=data[i].data.numpy())
                #visualize.plot_image_and_heatmap(output[3][i][8].data.numpy(),
                #                                 data=data[i].data.numpy(),
                #                                 title=filenamebase)
                #visualize.savefig('/home/paulo/' + filenamebase.replace('/', '_') + '_heatmap')

                labels_colorspace = conv.heatmaps_to_joints_colorspace(output[3][i].data.numpy())
                data_crop, crop_coords, labels_heatmaps, labels_colorspace = \
                    converter.crop_image_get_labels(data[i].data.numpy(), labels_colorspace, range(21))
                visualize.plot_image(data_crop, title=filenamebase, fig=fig)
                visualize.plot_joints_from_colorspace(labels_colorspace, title=filenamebase, fig=fig, data=data_crop)
                #visualize.savefig('/home/paulo/' + filenamebase.replace('/', '_') + '_crop')
                visualize.show()

        #loss.backward()
        valid_vars['total_loss'] += loss
        # accumulate pixel dist loss for sub-mini-batch
        valid_vars['total_pixel_loss'] = my_losses.accumulate_pixel_dist_loss_multiple(
            valid_vars['total_pixel_loss'], output[3], target_heatmaps, control_vars['batch_size'])
        if valid_vars['cross_entropy']:
            valid_vars['total_pixel_loss_sample'] = my_losses.accumulate_pixel_dist_loss_from_sample_multiple(
                valid_vars['total_pixel_loss_sample'], output[3], target_heatmaps, control_vars['batch_size'])
        else:
            valid_vars['total_pixel_loss_sample'] = [-1] * len(model.joint_ixs)
        # get boolean variable stating whether a mini-batch has been completed
        minibatch_completed = (batch_idx+1) % control_vars['iter_size'] == 0
        if minibatch_completed:
            # append total loss
            valid_vars['losses'].append(valid_vars['total_loss'].item())
            # erase total loss
            total_loss = valid_vars['total_loss'].item()
            valid_vars['total_loss'] = 0
            # append dist loss
            valid_vars['pixel_losses'].append(valid_vars['total_pixel_loss'])
            # erase pixel dist loss
            valid_vars['total_pixel_loss'] = [0] * len(model.joint_ixs)
            # append dist loss of sample from output
            valid_vars['pixel_losses_sample'].append(valid_vars['total_pixel_loss_sample'])
            # erase dist loss of sample from output
            valid_vars['total_pixel_loss_sample'] = [0] * len(model.joint_ixs)
            # check if loss is better
            if valid_vars['losses'][-1] < valid_vars['best_loss']:
                valid_vars['best_loss'] = valid_vars['losses'][-1]
                #print_verbose("  This is a best loss found so far: " + str(valid_vars['losses'][-1]), verbose)
            # log checkpoint
            if control_vars['curr_iter'] % control_vars['log_interval'] == 0:
                trainer.print_log_info(model, optimizer, 1, total_loss, valid_vars, control_vars)
                model_dict = {
                    'model_state_dict': model.state_dict(),
                    'optimizer_state_dict': optimizer.state_dict(),
                    'control_vars': control_vars,
                    'train_vars': valid_vars,
                }
                trainer.save_checkpoint(model_dict,
                                        filename=valid_vars['checkpoint_filenamebase'] +
                                                 str(control_vars['num_iter']) + '.pth.tar')
            # print time lapse
            prefix = 'Validating (Epoch #' + str(1) + ' ' + str(control_vars['curr_epoch_iter']) + '/' +\
                     str(control_vars['tot_iter']) + ')' + ', (Batch ' + str(control_vars['batch_idx']+1) +\
                     '(' + str(control_vars['iter_size']) + ')' + '/' +\
                     str(control_vars['num_batches']) + ')' + ', (Iter #' + str(control_vars['curr_iter']) +\
                     '(' + str(control_vars['batch_size']) + ')' +\
                     ' - log every ' + str(control_vars['log_interval']) + ' iter): '
            control_vars['tot_toc'] = display_est_time_loop(control_vars['tot_toc'] + time.time() - start,
                                                            control_vars['curr_iter'], control_vars['num_iter'],
                                                            prefix=prefix)

            control_vars['curr_iter'] += 1
            control_vars['start_iter'] = control_vars['curr_iter'] + 1
            control_vars['curr_epoch_iter'] += 1


    return valid_vars, control_vars

model, optimizer, control_vars, valid_vars, train_control_vars = validator.parse_args(model_class=HALNet)
if valid_vars['use_cuda']:
    torch.set_default_tensor_type('torch.cuda.FloatTensor')

valid_loader = synthhands_handler.get_SynthHands_testloader(root_folder=valid_vars['root_folder'],
                                                             joint_ixs=model.joint_ixs,
                                                             heatmap_res=(320, 240),
                                                             batch_size=control_vars['max_mem_batch'],
                                                             verbose=control_vars['verbose'])
control_vars['num_batches'] = len(valid_loader)
control_vars['n_iter_per_epoch'] = int(len(valid_loader) / control_vars['iter_size'])
control_vars['num_iter'] = len(valid_loader)

control_vars['tot_iter'] = int(len(valid_loader) / control_vars['iter_size'])
control_vars['start_iter_mod'] = control_vars['start_iter'] % control_vars['tot_iter']

trainer.print_header_info(model, valid_loader, control_vars)

control_vars['curr_iter'] = 1
control_vars['curr_epoch_iter'] = 1

valid_vars['total_loss'] = 0
valid_vars['total_pixel_loss'] = [0] * len(model.joint_ixs)
valid_vars['total_pixel_loss_sample'] = [0] * len(model.joint_ixs)

valid_vars, control_vars = validate(valid_loader, model, optimizer, valid_vars, control_vars, control_vars['verbose'])