import torch
from torch.autograd import Variable
import synthhands_handler
import trainer
import time
from magic import display_est_time_loop
import losses as my_losses
from debugger import print_verbose
from HALNet_prior import HALNet_prior
import numpy as np

def train(train_loader, model, optimizer, train_vars, control_vars, verbose=True):
    curr_epoch_iter = 1
    for batch_idx, (data, target) in enumerate(train_loader):
        control_vars['batch_idx'] = batch_idx
        if batch_idx < control_vars['iter_size']:
            print_verbose("\rPerforming first iteration; current mini-batch: " +
                  str(batch_idx+1) + "/" + str(control_vars['iter_size']), verbose, n_tabs=0, erase_line=True)
        # check if arrived at iter to start
        if control_vars['curr_epoch_iter'] < control_vars['start_iter_mod']:
            if batch_idx % control_vars['iter_size'] == 0:
                print_verbose("\rGoing through iterations to arrive at last one saved... " +
                      str(int(control_vars['curr_epoch_iter']*100.0/control_vars['start_iter_mod'])) + "% of " +
                      str(control_vars['start_iter_mod']) + " iterations (" +
                      str(control_vars['curr_epoch_iter']) + "/" + str(control_vars['start_iter_mod']) + ")",
                              verbose, n_tabs=0, erase_line=True)
                control_vars['curr_epoch_iter'] += 1
                control_vars['curr_iter'] += 1
                curr_epoch_iter += 1
            continue
        # save checkpoint after final iteration
        if control_vars['curr_iter'] == control_vars['num_iter']:
            print_verbose("\nReached final number of iterations: " + str(control_vars['num_iter']), verbose)
            print_verbose("\tSaving final model checkpoint...", verbose)
            final_model_dict = {
                'model_state_dict': model.state_dict(),
                'optimizer_state_dict': optimizer.state_dict(),
                'control_vars': control_vars,
                'train_vars': train_vars,
            }
            trainer.save_checkpoint(final_model_dict,
                            filename=train_vars['checkpoint_filenamebase'] +
                                     'final' + str(control_vars['num_iter']) + '.pth.tar')
            control_vars['done_training'] = True
            break
        # start time counter
        start = time.time()
        # get data and targetas cuda variables
        target_heatmaps, target_joints, _, target_prior = target
        data, target_heatmaps, target_prior = Variable(data), Variable(target_heatmaps), Variable(target_prior)
        if train_vars['use_cuda']:
            data = data.cuda()
            target_heatmaps = target_heatmaps.cuda()
            target_prior = target_prior.cuda()
        # visualize if debugging
        # get model output
        output = model(data)
        # accumulate loss for sub-mini-batch
        if train_vars['cross_entropy']:
            loss_func = my_losses.cross_entropy_loss_p_logq
        else:
            loss_func = my_losses.euclidean_loss
        loss, loss_prior = my_losses.calculate_loss_HALNet_prior(loss_func,
            output, target_heatmaps, target_prior, model.joint_ixs, model.WEIGHT_LOSS_INTERMED1,
            model.WEIGHT_LOSS_INTERMED2, model.WEIGHT_LOSS_INTERMED3,
            model.WEIGHT_LOSS_MAIN, control_vars['iter_size'])
        loss.backward()
        train_vars['total_loss'] += loss
        train_vars['total_loss_prior'] += loss_prior
        # accumulate pixel dist loss for sub-mini-batch
        train_vars['total_pixel_loss'] = my_losses.accumulate_pixel_dist_loss_multiple(
            train_vars['total_pixel_loss'], output[3], target_heatmaps, control_vars['batch_size'])
        if train_vars['cross_entropy']:
            train_vars['total_pixel_loss_sample'] = my_losses.accumulate_pixel_dist_loss_from_sample_multiple(
                train_vars['total_pixel_loss_sample'], output[3], target_heatmaps, control_vars['batch_size'])
        else:
            train_vars['total_pixel_loss_sample'] = [-1] * len(model.joint_ixs)
        # get boolean variable stating whether a mini-batch has been completed
        minibatch_completed = (batch_idx+1) % control_vars['iter_size'] == 0
        if minibatch_completed:
            # optimise for mini-batch
            optimizer.step()
            # clear optimiser
            optimizer.zero_grad()
            # append total loss
            train_vars['losses'].append(train_vars['total_loss'].data[0])
            # erase total loss
            total_loss = train_vars['total_loss'].data[0]
            train_vars['total_loss'] = 0
            # append total loss prior
            train_vars['losses_prior'].append(train_vars['total_loss_prior'].data[0])
            # erase total loss
            total_loss_prior = train_vars['total_loss_prior'].data[0]
            train_vars['total_loss_prior'] = 0
            # append dist loss
            train_vars['pixel_losses'].append(train_vars['total_pixel_loss'])
            # erase pixel dist loss
            train_vars['total_pixel_loss'] = [0] * len(model.joint_ixs)
            # append dist loss of sample from output
            train_vars['pixel_losses_sample'].append(train_vars['total_pixel_loss_sample'])
            # erase dist loss of sample from output
            train_vars['total_pixel_loss_sample'] = [0] * len(model.joint_ixs)
            # check if loss is better
            if train_vars['losses'][-1] < train_vars['best_loss']:
                train_vars['best_loss'] = train_vars['losses'][-1]
                print_verbose("  This is a best loss found so far: " + str(train_vars['losses'][-1]), verbose)
                train_vars['best_model_dict'] = {
                    'model_state_dict': model.state_dict(),
                    'optimizer_state_dict': optimizer.state_dict(),
                    'control_vars': control_vars,
                    'train_vars': train_vars,
                }
            if train_vars['losses_prior'][-1] < train_vars['best_loss_prior']:
                train_vars['best_loss_prior'] = train_vars['losses_prior'][-1]
            # log checkpoint
            if control_vars['curr_iter'] % control_vars['log_interval'] == 0:
                trainer.print_log_info(model, optimizer, epoch, total_loss, train_vars, control_vars)
                msg = ''
                msg += print_verbose(
                    "-------------------------------------------------------------------------------------------",
                    verbose) + "\n"
                msg += print_verbose("Current loss (prior): " + str(total_loss_prior), verbose) + "\n"
                msg += print_verbose("Best loss (prior): " + str(train_vars['best_loss_prior']), verbose) + "\n"
                msg += print_verbose("Mean total loss (prior): " + str(np.mean(train_vars['losses_prior'])), verbose) + "\n"
                msg += print_verbose("Mean loss (prior) for last " + str(control_vars['log_interval']) +
                                     " iterations (average total loss): " + str(
                    np.mean(train_vars['losses_prior'][-control_vars['log_interval']:])), verbose) + "\n"
                msg += print_verbose(
                    "-------------------------------------------------------------------------------------------",
                    verbose) + "\n"
                if not control_vars['output_filepath'] == '':
                    with open(control_vars['output_filepath'], 'a') as f:
                        f.write(msg + '\n')

            if control_vars['curr_iter'] % control_vars['log_interval_valid'] == 0:
                print_verbose("\nSaving model and checkpoint model for validation", verbose)
                checkpoint_model_dict = {
                    'model_state_dict': model.state_dict(),
                    'optimizer_state_dict': optimizer.state_dict(),
                    'control_vars': control_vars,
                    'train_vars': train_vars,
                }
                trainer.save_checkpoint(checkpoint_model_dict,
                                        filename=train_vars['checkpoint_filenamebase'] + 'for_valid_' +
                                                 str(control_vars['curr_iter']) + '.pth.tar')

            # print time lapse
            prefix = 'Training (Epoch #' + str(epoch) + ' ' + str(control_vars['curr_epoch_iter']) + '/' +\
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


    return train_vars, control_vars

model, optimizer, control_vars, train_vars = trainer.get_vars(model_class=HALNet_prior)
if train_vars['use_cuda']:
    torch.set_default_tensor_type('torch.cuda.FloatTensor')

train_loader = synthhands_handler.get_SynthHands_trainloader(root_folder=train_vars['root_folder'],
                                                             joint_ixs=model.joint_ixs,
                                                             heatmap_res=(320, 240),
                                                             batch_size=control_vars['max_mem_batch'],
                                                             verbose=control_vars['verbose'],
                                                             dataset_type='prior')
control_vars['num_batches'] = len(train_loader)
control_vars['n_iter_per_epoch'] = int(len(train_loader) / control_vars['iter_size'])

control_vars['tot_iter'] = int(len(train_loader) / control_vars['iter_size'])
control_vars['start_iter_mod'] = control_vars['start_iter'] % control_vars['tot_iter']

trainer.print_header_info(model, train_loader, control_vars)

model.train()
control_vars['curr_iter'] = 1

train_vars['best_loss_prior'] = 1e10
train_vars['losses_prior'] = []
train_vars['total_loss_prior'] = 0
for epoch in range(control_vars['num_epochs']):
    control_vars['curr_epoch_iter'] = 1
    if epoch + 1 < control_vars['start_epoch']:
        print_verbose("Advancing through epochs: " + str(epoch + 1), control_vars['verbose'], erase_line=True)
        control_vars['curr_iter'] += control_vars['n_iter_per_epoch']
        continue
    train_vars['total_loss'] = 0
    train_vars['total_pixel_loss'] = [0] * len(model.joint_ixs)
    train_vars['total_pixel_loss_sample'] = [0] * len(model.joint_ixs)
    optimizer.zero_grad()
    # train model
    train_vars, control_vars = train(train_loader, model, optimizer, train_vars, control_vars, control_vars['verbose'])
    if control_vars['done_training']:
        print_verbose("Done training.", control_vars['verbose'])
        break