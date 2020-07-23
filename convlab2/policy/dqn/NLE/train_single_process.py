# -*- coding: utf-8 -*-
import sys, os
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import numpy as np
import random
import torch
from torch import multiprocessing as mp
from convlab2.dialog_agent.agent import PipelineAgent
from convlab2.dialog_agent.env import Environment
from convlab2.dst.rule.multiwoz import RuleDST
from convlab2.policy.rule.multiwoz import RulePolicy
from convlab2.policy.dqn.NLE.NLE import NLE
from convlab2.policy.dqn.NLE.DQfD import DQfD
from convlab2.policy.DQNModule import read_action_map, expert_act_vec2ind, Transition_NLE, ExperienceReplayNLE
from convlab2.policy.vector.vector_multiwoz import MultiWozVector
from convlab2.evaluator.multiwoz_eval import MultiWozEvaluator
from argparse import ArgumentParser
import logging

DEVICE = torch.device("cuda" if torch.cuda.is_available() else "cpu")


def sampler(env, policy, batchsz, expert):
    """
    This is a sampler function, and it will be called by multiprocess.Process to sample data from environment by multiple
    processes.
    :param env: environment instance
    :param policy: policy network, to generate action from current policy
    :param batchsz: total sampled items
    :param expert: True/False means if an expert policy is used
    :return:
    """
    buff = ExperienceReplayNLE(100000)

    # we need to sample batchsz of (state, action, next_state, reward, mask)
    # each trajectory contains `trajectory_len` num of items, so we only need to sample
    # `batchsz//trajectory_len` num of trajectory totally
    # the final sampled number may be larger than batchsz.

    sampled_num = 0
    traj_len = 20  # max trajectory length

    while sampled_num < batchsz:
        # for each trajectory, we reset the env and get initial state
        s = env.reset()
        real_traj_len = 0   # real trajectory length of current sample dialog
        if expert:
            tmp_buff = ExperienceReplayNLE(100)
            tot_reward = 0
        for _ in range(traj_len):
            # for expert policy
            s_vec = torch.Tensor(policy.vector.state_vectorize(s))
            if expert:
                # [s_dim] => [a_dim]
                a, a_ind, candidate_act_ind = policy.predict(s)
            else:
                # [s_dim] => [a_dim]
                a, a_ind = policy.predict_ind(s)

            # interact with env
            next_s, r, done = env.step(a)

            # a flag indicates ending or not
            mask = 0 if done else 1
            next_s_vec = torch.Tensor(policy.vector.state_vectorize(next_s))
            if expert:
                # if expert action transformed to existing action space successfully, add this transition to expert demo
                tmp_buff.add_demo(s_vec.numpy(), a_ind, r, next_s_vec.numpy(), mask, 1, candidate_act_ind)
                tot_reward += r
            else:
                # add this transition to real experience memory
                buff.push(s_vec.numpy(), a_ind, r, next_s_vec.numpy(), mask, 0, [a_ind])
                real_traj_len += 1
            # update per step
            s = next_s
            # if dialog terminated then break
            if done:
                break
        if expert and tot_reward >= 70:
            buff.append(tmp_buff, True)
            real_traj_len += len(tmp_buff.expert_demo)
        # this is end of one trajectory
        sampled_num += real_traj_len
        if expert:
            logging.debug('<<Expert>> This dialogue got {} reward in total and {} frames have been sampled.'.format(tot_reward, sampled_num))
    return buff


def pretrain(env, expert_policy, policy, batchsz):
    """
    pre-train agent policy
    :param env:
    :param expert_policy:
    :param policy:
    :param batchsz:
    :param process_num:
    :return:
    """
    # initialize pre-fill replay buffer
    prefill_buff = ExperienceReplayNLE(15000)
    sampled_frames_num = 0  # sampled number of frames
    sampled_success_num = 0  # sampled number of dialogs
    pre_train_frames_num = 15000  # total number of dialogs required to sample
    seed = 20200721
    while len(prefill_buff.expert_demo) < pre_train_frames_num:
        random.seed(seed)
        np.random.seed(seed)
        torch.manual_seed(seed)
        seed += 1
        # achieve a buffer stored expert demonstrations
        new_buff = sampler(env, expert_policy, batchsz, True)
        cur_frames_num = len(list(new_buff.get_batch().mask))
        cur_success_num = list(new_buff.get_batch().reward).count(80)
        # put expert demonstrations to pre-fill buffer
        prefill_buff.append(new_buff, True)
        logging.debug('<<Replay Buffer>> At this turn, {} frames sampled with {} successful dialogues, now pre-fill '
                      'buffer has {} transitions in total'.format(cur_frames_num, cur_success_num, len(prefill_buff.expert_demo)))
    while sampled_frames_num < pre_train_frames_num:
        random.seed(seed)
        np.random.seed(seed)
        torch.manual_seed(seed)
        seed += 1
        # achieve a buffer stored expert demonstrations
        new_buff = sampler(env, expert_policy, batchsz, True)
        cur_frames_num = len(list(new_buff.get_batch().mask))
        cur_success_num = list(new_buff.get_batch().reward).count(80)
        # put expert demonstrations to pre-fill buffer
        prefill_buff.append(new_buff, True)
        pre_train_loss = 0

        # sample 2000 batches
        for _ in range(3000):
            # each batch size is 32
            batch = prefill_buff.get_batch(32)
            s = torch.from_numpy(np.stack(batch.state)).type(torch.float).to(device=DEVICE)
            a = torch.from_numpy(np.stack(batch.action)).type(torch.long).to(device=DEVICE)
            r = torch.from_numpy(np.stack(batch.reward)).type(torch.float).to(device=DEVICE)
            s_next = torch.from_numpy(np.stack(batch.next_state)).type(torch.float).to(device=DEVICE)
            mask = torch.Tensor(np.stack(batch.mask)).type(torch.float).to(device=DEVICE)
            expert_label = np.stack(batch.expert_label)
            candidate_a_ind = np.array(batch.candidate_act_ind)
            # compute loss for current batch
            cur_loss = policy.compute_loss(s, a, r, s_next, mask, expert_label, candidate_a_ind)
            pre_train_loss += cur_loss
            # update
            policy.update(cur_loss)
        # update target network
        policy.update_net()
        sampled_frames_num += cur_frames_num
        sampled_success_num += cur_success_num

        logging.debug('<<dialog policy DQfD pre-train>> {} frames sampled with {} successful dialogues, learning rate '
                      '{}, loss {}'.format(sampled_frames_num, sampled_success_num, policy.scheduler.get_last_lr()[0], pre_train_loss/3000))
        # decay learning rate
        policy.scheduler.step()
    return prefill_buff


def train_update(prefill_buff, env, policy, batchsz, epoch):
    seed = epoch
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    # achieve a buffer stored real agent experience
    new_buff = sampler(env, policy, batchsz, False)
    cur_frames_num = len(list(new_buff.get_batch().reward))
    cur_success_num = list(new_buff.get_batch().reward).count(80)
    # put real agent experience to pre-fill buffer while keep total transition number under maximum (100,000)
    prefill_buff.append(new_buff, False)
    train_loss = 0
    # linearly decrease epsilon
    if epoch < policy.epsilon_degrade_period:
        policy.epsilon = policy.epsilon_init - epoch * (policy.epsilon_init - policy.epsilon_final) / policy.epsilon_degrade_period
    else:
        policy.epsilon = policy.epsilon_final

    if (epoch+1) % 10 == 0:
        # update target network
        policy.update_net()

    # sample 2000 batches
    for _ in range(3000):
        # each batch size is 32
        batch = prefill_buff.get_batch(32)
        s = torch.from_numpy(np.stack(batch.state)).type(torch.float).to(device=DEVICE)
        a = torch.from_numpy(np.stack(batch.action)).type(torch.long).to(device=DEVICE)
        r = torch.from_numpy(np.stack(batch.reward)).type(torch.float).to(device=DEVICE)
        s_next = torch.from_numpy(np.stack(batch.next_state)).type(torch.float).to(device=DEVICE)
        mask = torch.Tensor(np.stack(batch.mask)).type(torch.float).to(device=DEVICE)
        expert_label = np.stack(batch.expert_label)
        candidate_a_ind = np.array(batch.candidate_act_ind)
        # compute loss for current batch
        cur_loss = policy.compute_loss(s, a, r, s_next, mask, expert_label, candidate_a_ind)
        train_loss += cur_loss
        # update
        policy.update(cur_loss)

    if epoch % 10 == 0:
        logging.debug('<<dialog policy DQfD train>> epoch {}, {} frames sampled with {} successful '
                      'dialogues at this turn, lr {}, loss: {}'.format(epoch, cur_frames_num, cur_success_num,
                                                                       policy.scheduler.get_last_lr()[0], train_loss/3000))
    # decay learning rate
    if policy.scheduler.get_last_lr()[0] > policy.min_lr:
        policy.scheduler.step()
    if epoch % 10 == 0:
        # save current model
        policy.save(os.path.join(root_dir, 'convlab2/policy/dqn/NLE/save'), epoch)


def generate_necessary_file(root_dir):
    voc_file = os.path.join(root_dir, 'data/multiwoz/sys_da_voc.txt')
    voc_opp_file = os.path.join(root_dir, 'data/multiwoz/usr_da_voc.txt')
    vector = MultiWozVector(voc_file, voc_opp_file)
    action_map_file = os.path.join(root_dir, 'convlab2/policy/act_500_list.txt')
    act2ind_dict, ind2act_dict = read_action_map(action_map_file)
    return vector, act2ind_dict, ind2act_dict


if __name__ == '__main__':
    parser = ArgumentParser()
    parser.add_argument("--load_path", type=str, default="", help="path of model to load")
    parser.add_argument("--batchsz", type=int, default=1000, help="batch size of trajactory sampling")
    parser.add_argument("--epoch", type=int, default=2550, help="number of epochs to train")
    parser.add_argument("--process_num", type=int, default=1, help="number of processes of trajactory sampling")
    args = parser.parse_args()

    root_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))
    vector, act2ind_dict, ind2act_dict = generate_necessary_file(root_dir)
    # simple rule DST
    dst_usr = None
    dst_sys = RuleDST()
    # load policy sys
    policy_sys = DQfD(True)
    policy_sys.load(args.load_path)
    # rule-based expert
    expert_policy = NLE()
    # rule policy
    policy_usr = RulePolicy(character='usr')
    # assemble
    simulator = PipelineAgent(None, None, policy_usr, None, 'user')
    # evaluator = MultiWozEvaluator()
    env = Environment(None, simulator, None, dst_sys)
    # pre-train
    prefill_buff = pretrain(env, expert_policy, policy_sys, args.batchsz)
    prefill_buff.max_size = 100000
    # real_experience_fill(prefill_buff, env, policy_sys, vector, act2ind_dict, args.batchsz, args.process_num)
    for i in range(args.epoch):
        # train
        train_update(prefill_buff, env, policy_sys, args.batchsz, i)