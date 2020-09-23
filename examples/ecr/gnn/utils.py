import numpy as np
import torch, sys, argparse
import json
import ast, math
from collections import defaultdict, OrderedDict
from maro.simulator.scenarios.ecr.common import Action, DecisionEvent
from maro.simulator import Env
import shutil
from maro.utils import convert_dottable, clone
import io, yaml, os, shutil
import datetime
from torch.optim.lr_scheduler import LambdaLR
from torch.optim import SGD
import random

def compute_v2p_degree_matrix(env):
    '''
    this function compute the adjacent matrix 
    '''
    topo_config = env.configs
    static_dict = env.summary['node_mapping']['ports']
    dynamic_dict = env.summary['node_mapping']['vessels']
    adj_matrix = np.zeros((len(dynamic_dict), len(static_dict)), dtype=np.int)
    for v, vinfo in topo_config['vessels'].items():
        route_name = vinfo['route']['route_name']
        route = topo_config['routes'][route_name]
        vid = dynamic_dict[v]
        for p in route:
            adj_matrix[vid][static_dict[p['port_name']]] += 1
    
    return adj_matrix

def warm_up_lr(opt, warmup_steps):
    warm_up = lambda ep: math.pow(warmup_steps, 0.5) * min(math.pow(ep+1, -0.5), math.pow(warmup_steps, -1.5)*(ep+1))
    return LambdaLR(opt, warm_up)

def from_numpy(device, *np_values):
    return [torch.from_numpy(v).to(device) for v in np_values]

def gnn_union(p, po, pedge, v, vo, vedge, p2p, ppedge, seq_mask, device):
    '''
    v: (seq_len, batch, v_cnt, v_dim)
    vo: (batch, v_cnt, p_cnt)
    vedge: (batch, v_cnt, p_cnt, e_dim)
    '''

    seq_len, batch, v_cnt, v_dim = v.shape
    _, _, p_cnt, p_dim = p.shape

    p, po, pedge, v, vo, vedge, p2p, ppedge, seq_mask = from_numpy(device, p, po, pedge, 
                                                                    v, vo, vedge, p2p, ppedge, seq_mask)

    batch_range = torch.arange(batch, dtype=torch.long).to(device)
    # vadj.shape: (batch*v_cnt, p_cnt*)
    vadj, vedge = flatten_embedding(vo, batch_range, vedge)
    # vmask.shape: (batch*v_cnt, p_cnt*)
    vmask = vadj == 0
    # vadj.shape: (p_cnt*, batch*v_cnt)
    vadj = vadj.transpose(0, 1)
    # vedge.shape: (p_cnt*, batch*v_cnt, e_dim)
    vedge = vedge.transpose(0, 1)
    # vedge = vedge.reshape(-1, *vedge.shape[-2:]).transpose(0, 1)[:vadj.shape[0]]

    padj, pedge = flatten_embedding(po, batch_range, pedge)
    pmask = padj == 0
    padj = padj.transpose(0, 1)
    pedge = pedge.transpose(0, 1)
    # pedge = pedge.reshape(-1, *pedge.shape[-2:]).transpose(0, 1)[:padj.shape[0]]
    
    p2p_adj = p2p.repeat(batch, 1, 1)
    # p2p_adj.shape: (batch*p_cnt, p_cnt*)
    p2p_adj, ppedge = flatten_embedding(p2p_adj, batch_range, ppedge)
    # p2p_mask.shape: (batch*p_cnt, p_cnt*)
    p2p_mask = p2p_adj == 0
    # p2p_adj.shape: (p_cnt*, batch*p_cnt)
    p2p_adj = p2p_adj.transpose(0, 1)
    ppedge = ppedge.transpose(0, 1)

    return {
        'v': v,
        'p': p,
        'pe': {
            'edge': pedge,
            'adj': padj,
            'mask': pmask,
        },
        've': {
            'edge': vedge,
            'adj': vadj,
            'mask': vmask,
        },
        'ppe': {
            'edge': ppedge,
            'adj': p2p_adj,
            'mask': p2p_mask,
        },
        'mask': seq_mask,
    }


def flatten_embedding(embedding, batch_range, edge=None):
    if len(embedding.shape) == 3:
        batch, x_cnt, y_cnt = embedding.shape
        addon = (batch_range*y_cnt).view(batch, 1, 1)
    else:
        seq_len, batch, x_cnt, y_cnt = embedding.shape
        addon = (batch_range*y_cnt).view(seq_len, batch, 1, 1)

    embedding_mask = embedding == 0
    embedding += addon
    embedding[embedding_mask] = 0
    ret = embedding.reshape(-1, embedding.shape[-1])
    col_mask = ret.sum(dim=0) != 0
    ret = ret[:, col_mask]
    if edge is None:
        return ret
    else:
        edge = edge.reshape(-1, *edge.shape[2:])[:, col_mask, :]
        return ret, edge

def log2json(file_path):
    """load the log file as a json list.
    """

    with open(file_path, 'r') as fp:
        lines = fp.read().splitlines()
        json_list = '[' + ','.join(lines) + ']'
        return ast.literal_eval(json_list)

def decision_cnt_analysis(env, pv=False, buffer_size=8):
    if not pv:
        decision_cnt = [buffer_size] * len(env.node_name_mapping['static'])
        r, pa, is_done = env.step(None)
        while not is_done:
            decision_cnt[pa.port_idx] += 1
            action = Action(pa.vessel_idx, pa.port_idx, 0)
            r, pa, is_done = env.step(action)
    else:
        decision_cnt = OrderedDict()
        r, pa, is_done = env.step(None)
        while not is_done:
            if (pa.port_idx, pa.vessel_idx) not in decision_cnt:
                decision_cnt[pa.port_idx, pa.vessel_idx] = buffer_size
            else:
                decision_cnt[pa.port_idx, pa.vessel_idx] += 1
            action = Action(pa.vessel_idx, pa.port_idx, 0)
            r, pa, is_done = env.step(action)
    env.reset()
    return decision_cnt

def random_shortage(env, tick, action_dim=21):
    zero_idx = action_dim//2
    r, pa, is_done = env.step(None)
    node_cnt = len(env.summary['node_mapping']['ports'])
    while not is_done:
        '''
        load, discharge = pa.action_scope.load, pa.action_scope.discharge
        action_idx = np.random.randint(action_dim) - zero_idx
        if action_idx < 0:
            actual_action = int(1.0*action_idx/zero_idx*load)
        else:
            actual_action = int(1.0*action_idx/zero_idx*discharge)
        '''
        # print(action_idx, -load, actual_action, discharge)
        action = Action(pa.vessel_idx, pa.port_idx, 0)
        r, pa, is_done = env.step(action)
    
    shs = env.snapshot_list['ports'][tick-1:list(range(node_cnt)):'acc_shortage']
    fus = env.snapshot_list['ports'][tick-1:list(range(node_cnt)):'acc_fulfillment']
    env.reset()
    return fus - shs, np.sum(shs+fus)

def return_scaler(env, tick, gamma, action_dim=21):
    R, tot_amount = random_shortage(env, tick, action_dim)
    Rs_mean = np.mean(R)/tick/(1-gamma)
    return abs(1.0/Rs_mean), tot_amount
    

def load_config(config_pth):
    with io.open(config_pth, 'r') as in_file:
        raw_config = yaml.safe_load(in_file)
        config = convert_dottable(raw_config)
    
    if config.env.seed < 0:
        config.env.seed = random.randint(0, 99999)

    regularize_config(config)
    return config

def save_config(config, config_pth):
    with open(config_pth, 'w') as fp:
        config = dottable2dict(config)
        config['env']['exp_per_ep'] = ['%d, %d, %d'%(k[0], k[1],d) for k, d in config['env']['exp_per_ep'].items()]
        yaml.safe_dump(config, fp)
    
def dottable2dict(config):
    if isinstance(config, float):
        return str(config)
    if not isinstance(config, dict):
        return clone(config)
    rt = {}
    for k, v in config.items():
        rt[k] = dottable2dict(v)
    return rt

def save_code(folder, save_pth):
    save_path = os.path.join(save_pth,'code')
    code_pth = os.path.join(os.getcwd(),folder)
    shutil.copytree(code_pth,save_path)


def fix_seed(env, seed):
    env.set_seed(seed)
    np.random.seed(seed)        
    random.seed(seed)

def zero_play(**args):
    env = Env(**args)
    static_mapping = env.node_name_mapping['static']
    r, pa, is_done = env.step(None)
    while not is_done:
        action = Action(pa.vessel_idx, pa.port_idx, 0)
        r, pa, is_done = env.step(action)
    return env.snapshot_list


def regularize_config(config):
    def parse_value(v):
        try:
            return int(v)
        except:
            try:
                return float(v)
            except:
                if v == 'false' or v == 'False':
                    return False
                elif v == 'true' or v == 'True':
                    return True
                else:
                    return v

    def set_attr(config, attrs, value):
        if len(attrs) == 1:
            config[attrs[0]] = value 
        else:
            set_attr(config[attrs[0]], attrs[1:], value)

    all_args = sys.argv[1:]
    for i in range(len(all_args)//2):
        name = all_args[i*2]
        attrs = name[2:].split('.')
        value = parse_value(all_args[i*2+1])
        set_attr(config, attrs, value)    

def analysis_speed(env):
    speed_dict = defaultdict(int)
    eq_speed = 0
    for ves in env.configs['vessels'].values():
        speed_dict[ves['sailing']['speed']] += 1
    for sp, cnt in speed_dict.items():
        eq_speed += 1.0*cnt/sp
    eq_speed = 1.0/eq_speed
    return speed_dict, eq_speed