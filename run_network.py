# -*- coding: utf-8 -*-
# (c) May 2018 Aditya Gilra, EPFL.

"""Extends code by Aditya Gilra. Some reservoir tweaks are inspired by Nicola and Clopath, arxiv, 2016 and Miconi 2016."""

import torch
import json
from config import Config
from error_computations import Error_computations
# from refactor.ofc_trailtype import OFC as OFC_Trial
from vmPFC_k_means import OFC
from plot_figures import *
from data_generator import data_generator
import os
import numpy as np
import matplotlib as mpl

mpl.rcParams['axes.spines.left'] = True
mpl.rcParams['axes.spines.right'] = False
mpl.rcParams['axes.spines.top'] = False
mpl.rcParams['axes.spines.bottom'] = True

mpl.rcParams['pdf.fonttype'] = 42
mpl.rcParams['ps.fonttype'] = 42

import matplotlib.pyplot as plt
# plt.ion()
# from IPython import embed; embed()
# import pdb; pdb.set_trace()
from scipy.io import savemat
import tqdm
import time
import plot_utils as pltu
import argparse
from model import PFCMD


def train(areas, data_gen, config):
    pfcmd, vmPFC = areas
    Ntrain = config.trials_per_block * config.Nblocks

    # Containers to save simulation variables
    wOuts = np.zeros(shape=(Ntrain, config.Nout, config.Npfc))
    wPFC2MDs = np.zeros(shape=(Ntrain, 2, config.Npfc))
    wMD2PFCs = np.zeros(shape=(Ntrain, config.Npfc, 2))
    wMD2PFCMults = np.zeros(shape=(Ntrain, config.Npfc, 2))
    MDpreTraces = np.zeros(shape=(Ntrain, config.Npfc))
    wJrecs = np.zeros(shape=(Ntrain, 40, 40))
    PFCrates = np.zeros((Ntrain, config.tsteps, config.Npfc))
    MDinputs = np.zeros((Ntrain, config.tsteps, config.Nmd))
    if config.neural_vmPFC:
        vm_MDinputs = np.zeros((Ntrain, config.tsteps, config.Nmd))
        vm_Outrates = np.zeros((Ntrain, vm_config.tsteps, vm_config.Nout))
    MDrates = np.zeros((Ntrain, config.tsteps, config.Nmd))
    Outrates = np.zeros((Ntrain, config.tsteps, config.Nout))
    Inputs = np.zeros((Ntrain, config.Ninputs+3)) # Adding OFC latents temp #TODO remove this.
    Targets = np.zeros((Ntrain, config.Nout))
    pfcmd.hx_of_ofc_signal_lengths = []
    MSEs = np.zeros(Ntrain)

    q_values_before = np.array([0.5, 0.5])
    for traini in tqdm.tqdm(range(Ntrain)):
        if traini % config.trials_per_block == 0:
            blocki = traini // config.trials_per_block
            association_level, ofc_control = next(data_gen.block_generator(
                blocki))  # Get the context index for this current block
        if config.debug:
            print('context i: ', association_level)

        cue, target = data_gen.trial_generator(association_level)

        # trigger OFC switch signal for a number of trials in the block
        # q_values_before = ofc.get_v()
        error_computations.Sabrina_Q_values = ofc.get_v() # TODO: this is just a temp fix to get estimates from Sabrina's vmPFC.

        _, routs, outs, MDouts, MDinps, errors = \
            pfcmd.run_trial(association_level, q_values_before, error_computations, cue, target, config, MDeffect=config.MDeffect,
                            train=config.train)

        switch = error_computations.update_v(cue, outs, target, MDouts.mean(axis=0), routs.mean(axis=0))
        config.ofc_effect = config.ofc_effect_momentum * config.ofc_effect
        if switch and (ofc_control is 'on'): 
            config.ofc_effect = config.ofc_effect_magnitude

        # if traini%50==0: ofc_plots(error_computations, traini, '_')

        ofc_signal = ofc.update_v(cue, outs[-1,:], target)
        if ofc_signal == "SWITCH":
            ofc.switch_context()
        q_values_after = ofc.get_v()

        if config.neural_vmPFC:
            out_trial_avg = outs.mean(axis=0)
            outs_centered = out_trial_avg- out_trial_avg.mean() +0.5
            matchness = np.dot(cue, outs_centered)

            vmPFC_input = np.array([matchness, q_values_before[0]])
            # _, routs, vm_outs, MDouts, MDinps, _ =\
            _, _, vm_outs, _, vm_MDinps,_ =\
            vmPFC.run_trial(association_level, q_values_before, error_computations_vmPFC,
                                vmPFC_input, q_values_after, vm_config, MDeffect=config.MDeffect,
                                train=config.train)

        # config.use_neural_q_values = True if bi > 6 else False  # take off training wheels for q_values learning
        config.use_neural_q_values = False
        if config.use_neural_q_values:
            q_values_before = vm_outs.mean(axis=0)
        else:
            q_values_before = ofc.get_v()

        # Collect variables for analysis, plotting, and saving to disk
        area_to_plot = pfcmd
        PFCrates[traini, :, :] = routs
        MDinputs[traini, :, :] = MDinps
        if config.neural_vmPFC:
            vm_MDinputs[traini, :, :] = vm_MDinps if area_to_plot is vmPFC else MDinps
            vm_Outrates[traini, :, :] = vm_outs 
        MDrates[traini, :, :] = MDouts
        Outrates[traini, :, :] = outs
        Inputs[traini, :] = np.concatenate([cue, q_values_after, error_computations.p_sm_snm_ns])
        Targets[traini, :] = target
        wOuts[traini, :, :] = area_to_plot.wOut
        wPFC2MDs[traini, :, :] = area_to_plot.wPFC2MD
        wMD2PFCs[traini, :, :] = area_to_plot.wMD2PFC
        wMD2PFCMults[traini, :, :] = area_to_plot.wMD2PFCMult
        MDpreTraces[traini, :] = area_to_plot.MDpreTrace
        MSEs[traini] += np.mean(errors*errors)
        if config.reinforceReservoir:
            # saving the whole rec is too large, 1000*1000*2200
            wJrecs[traini, :, :] = area_to_plot.Jrec[:40,
                                                0:25:1000].detach().cpu().numpy()

        # Saves a data file per each trial
        # TODO possible variables to add for Mante & Sussillo condition analysis:
        #   - association level, OFC values
        if config.args_dict["save_data_by_trial"]:
            trial_weights = {
                "w_outputs": wOuts[traini].tolist(),
                "w_PFC2MD": wPFC2MDs[traini].tolist(),
                "w_MD2PFCs": wMD2PFCs[traini].tolist(),
                "w_MD2PFC_mults": wMD2PFCMults[traini].tolist(),
                "w_MD_pretraces": MDpreTraces[traini].tolist()
            }
            trial_rates = {
                "r_PFC": PFCrates[traini].tolist(),
                "MD_input": MDinputs[traini].tolist(),
                "r_MD": MDrates[traini].tolist(),
                "r_output": Outrates[traini].tolist(),
            }
            trial_data = {
                "input": Inputs[traini].tolist(),
                "target": Targets[traini].tolist(),
                "mse": MSEs[traini]
            }

            d = f"{config.args_dict['outdir']}/{config.args_dict['exp_name']}/by_trial"
            if not os.path.exists(d):
                os.makedirs(d)
            with open(f"{d}/{traini}.json", 'w') as outfile:
                json.dump({"trial_data": trial_data,
                            "network_weights": trial_weights,
                            "network_rates": trial_rates}, outfile)

    # collect input from OFC and add it to Inputs for outputting.
    # if ofc is off, the trial gets 0, if it is stimulating the 'match' side, it gets 1
    # and 'non-match' gets -1. Although currently match and non-match have no meaning,
    # as MD can be responding to either match or non-match. The disambiguation happens in post analysis
    ofc_inputs = np.zeros((Ntrain,1))
    tpb = config.trials_per_block
    if len(pfcmd.hx_of_ofc_signal_lengths) > 0:
        for bi in range(config.Nblocks):
            ofc_hx = np.array(pfcmd.hx_of_ofc_signal_lengths)
            if bi in ofc_hx[:,0]:
                if data_generator.ofc_control_schedule[bi] is 'match':
                    ofc_inputs[bi*tpb:bi*tpb+config.no_of_trials_with_ofc_signal] = np.ones((config.no_of_trials_with_ofc_signal, 1))
                else:
                    ofc_inputs[bi*tpb:bi*tpb+config.no_of_trials_with_ofc_signal] = -np.ones((config.no_of_trials_with_ofc_signal, 1))
    Inputs = np.concatenate((Inputs, ofc_inputs), axis=-1)

    weights = [wOuts, wPFC2MDs, wMD2PFCs,
                wMD2PFCMults,  wJrecs, MDpreTraces]
    rates = [PFCrates, MDinputs, MDrates,
                Outrates, Inputs, Targets, MSEs]
    # plot_q_values([vm_Outrates, vm_MDinputs])
    plot_weights(area_to_plot, weights, config)
    plot_rates(area_to_plot, rates, config)
    plot_what_i_want(area_to_plot, weights, rates, config)
    # ofc_plots(error_computations, 2500, 'end_')
    #from IPython import embed; embed()
    dirname = config.args_dict['outdir'] + \
        "/"+config.args_dict['exp_name']+"/"
    parm_summary = str(list(config.args_dict.values())[0])+"_"+str(
        list(config.args_dict.values())[1])+"_"+str(
        list(config.args_dict.values())[2])+"_"+str(list(config.args_dict.values())[5])
    if not os.path.exists(dirname):
        os.makedirs(dirname)
    def fn(fn_str): return os.path.join(dirname, 'fig_{}_{}_{}.{}'.format(
        fn_str, parm_summary, time.strftime("%Y%m%d-%H%M%S"), config.figure_format))

    if config.plotFigs:  # Plotting and writing results. Needs cleaned up.
        area_to_plot.figWeights.savefig(fn('weights'),  transparent=True,dpi=pltu.fig_dpi,
                                facecolor='w', edgecolor='w', format=config.figure_format)
        area_to_plot.figOuts.savefig(fn('behavior'),  transparent=True,dpi=pltu.fig_dpi,
                                facecolor='w', edgecolor='w', format=config.figure_format)
        area_to_plot.figRates.savefig(fn('rates'),    transparent=True,dpi=pltu.fig_dpi,
                                facecolor='w', edgecolor='w', format=config.figure_format)
        if config.debug:
            area_to_plot.figTrials.savefig(fn('trials'),  transparent=True,dpi=pltu.fig_dpi,
                                    facecolor='w', edgecolor='w', format=config.figure_format)
            area_to_plot.fig_monitor = plt.figure()
            area_to_plot.monitor.plot(area_to_plot.fig_monitor, area_to_plot)
            area_to_plot.figCustom.savefig(
                fn('custom'), dpi=pltu.fig_dpi, facecolor='w', edgecolor='w', format=config.figure_format)
            area_to_plot.fig_monitor.savefig(
                fn('monitor'), dpi=pltu.fig_dpi, facecolor='w', edgecolor='w', format=config.figure_format)

        # output some variables of interest:
        # md ampflication and % correct responses from model.
    filename7 = os.path.join(dirname, 'values_of_interest.txt')
    filename7exits = os.path.exists(filename7)
    with open(filename7, 'a') as f:
        if not filename7exits:
            [f.write(head+'\t') for head in ['switches', 'LR', 'ofc',
                                                'HebbT', '1st', '2nd', '3rd', '4th', 'avg1-3', 'mean', 'PFCavgFR\n']]
        [f.write('{}\t '.format(val))
            for val in [*config.args_dict.values()][:3] + [list(config.args_dict.values())[5]] ]
        # {:.2e} \t {:.2f} \t'.format(config.args_dict['switches'], config.args_dict['MDlr'],config.args_dict['MDactive'] ))
        for score in area_to_plot.score:
            f.write('{:.2f}\t'.format(score))
        f.write('{:.2f}\t'.format(PFCrates.mean()))
        f.write('\n')

    np.save(fn('saved_Corrects')[:-4]+'.npy', area_to_plot.corrects)
    if config.saveData:  # output massive weight and rate files
        import pickle
        filehandler = open(fn('saved_rates')[:-4]+'.pickle', 'wb')
        pickle.dump(rates, filehandler)
        filehandler.close()
        filehandler = open(fn('saved_weights')[:-4]+'.pickle', 'wb')
        pickle.dump(weights, filehandler)
        filehandler.close()

        # np.save(os.path.join(dirname, 'Rates{}_{}'.format(parm_summary, time.strftime("%Y%m%d-%H%M%S"))), rates)
        # np.save(os.path.join(dirname, 'Weights{}_{}'.format(parm_summary, time.strftime("%Y%m%d-%H%M%S"))), weights)



###################################################################################
###################################################################################
###################################################################################

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    group = parser.add_argument("exp_name", default="new_code",
                                nargs='?',  type=str, help="pass a str for experiment name")
    group = parser.add_argument(
        "seed", default=2, nargs='?',  type=float, help="simulation seed")
    
    group = parser.add_argument(
        "--var1", default=1, nargs='?', type=float, help="arg_1")
    group = parser.add_argument(
        "--var2", default=1.0, nargs='?', type=float, help="arg_2")
    group = parser.add_argument(
        "--var3", default=5.0, nargs='?', type=float, help="arg_3")
    group = parser.add_argument("--outdir", default="./results",
                                nargs='?',  type=str, help="pass a str for data directory")
    args = parser.parse_args()
    # can  assign args.x and args.y to vars
    # OpenMind shared directory: "/om2/group/halassa/PFCMD-ali-sabrina"
    args_dict = {'MDeffect': args.var1 , 'Gcompensation': args.var2, 'OFC_effect': args.var3,
                 'outdir':  args.outdir, 'exp_name': args.exp_name, 'seed': int(args.seed),
                 "save_data_by_trial": False} # 'MDlr': args.y,'switches': args.x,  'MDactive': args.z,

    config = Config(args_dict)
    vm_config = Config(args_dict)
    # vm_config.Ninputs = 6
    data_generator = data_generator(config)

    config.MDremovalCompensationFactor = args_dict['Gcompensation']
    config.MDeffect = bool(args_dict['MDeffect'])
    config.ofc_effect_magnitude = args_dict['OFC_effect']

    ofc = OFC()
    ofc_vmPFC = OFC()
    error_computations = Error_computations(config)
    error_computations_vmPFC = Error_computations(vm_config)

    # redefine some parameters for quick experimentation here.
    # config.no_of_trials_with_ofc_signal = int(args_dict['switches'])
    # config.MDamplification = 30.  # args_dict['switches']
    # config.MDlearningBiasFactor = args_dict['MDactive']

    pfcmd = PFCMD(config)
    if config.neural_vmPFC:
        vmPFC = PFCMD(vm_config)
    else:
        vmPFC = []

        
    if config.reLoadWeights:
        filename = 'dataPFCMD/data_reservoir_PFC_MD' + '_R'+str(pfcmd.RNGSEED) + '.shelve'
        pfcmd.load(filename)
    t = time.perf_counter()

    train((pfcmd, vmPFC), data_generator, config)
    
    print('training_time', (time.perf_counter() - t)/60, ' minutes')
    
    if config.saveData:
        pfcmd.save()
        pfcmd.fileDict.close()

