import torch
import config
import time
import torch.backends.cudnn as cudnn
import models
import torch.optim as optim
import os
import datetime
import argparse
import itertools
from torch import nn
from torch.optim.lr_scheduler import MultiStepLR, ReduceLROnPlateau
from data import *
from utils import *
from metrics import *

cudnn.benchmark = True
parser = argparse.ArgumentParser(description='Config')
config.init()
for k in config.PARAM:
    exec('{0} = config.PARAM[\'{0}\']'.format(k))
    exec('parser.add_argument(\'--{0}\',default=config.PARAM[\'{0}\'], help=\'\')'.format(k))
args = vars(parser.parse_args())
for k in config.PARAM:
    if(config.PARAM[k]!=args[k]):
        exec('config.PARAM[\'{0}\'] = {1}'.format(k,args[k]))

def main():
    print(config.PARAM)
    #resume_TAG_mode = [['full','half'],['dis','sep'],['subset','class'],['2','4','8','10']]
    resume_TAG_mode = [['full','half'],['sin'],['class'],['2','4','8','10']]
    resume_TAGs = list(itertools.product(*resume_TAG_mode))
    seeds = list(range(init_seed,init_seed+num_Experiments))
    result = {}
    for i in range(len(resume_TAGs)):    
        resume_TAG = "_".join(list(resume_TAGs[i]))
        for j in range(num_Experiments):
            resume_model_TAG = '{}_{}_{}'.format(seeds[j],model_data_name,model_name) if(resume_TAG=='') else '{}_{}_{}_{}'.format(seeds[j],model_data_name,model_name,resume_TAG)
            model_TAG = resume_model_TAG if(special_TAG=='') else '{}_{}'.format(resume_model_TAG,special_TAG)
            print('Experiment: {}'.format(model_TAG))
            result[model_TAG] = runExperiment(model_TAG)
            save(result[model_TAG],'./output/result/{}.pkl'.format(model_TAG)) 
    return

def runExperiment(model_TAG):
    model_TAG_list = model_TAG.split('_')
    seed = int(model_TAG_list[0])
    if(model_TAG_list[-3]=='dis'):
        config.PARAM['num_node'] = {'E':int(model_TAG_list[-1]),'D':0}
    elif(model_TAG_list[-3]=='sep'):
        config.PARAM['num_node'] = {'E':int(model_TAG_list[-1]),'D':int(model_TAG_list[-1])}
    elif(model_TAG_list[-3]=='sin'):
        config.PARAM['num_node'] = {'E':0,'D':0}
    if(model_TAG_list[-2]=='subset'):
        config.PARAM['num_class'] = 0
    elif(model_TAG_list[-2]=='class'):
        config.PARAM['num_class'] = int(model_TAG_list[-1])
    torch.manual_seed(seed)
    torch.cuda.manual_seed(seed)
    randomGen = np.random.RandomState(seed)
    
    train_dataset,test_dataset = fetch_dataset(data_name=test_data_name)
    valid_data_size = len(train_dataset) if(data_size==0) else data_size
    _,test_loader = split_dataset(train_dataset,test_dataset,valid_data_size,batch_size=batch_size,radomGen=randomGen)
    best = load('./output/model/{}_best.pkl'.format(model_TAG))
    last_epoch = best['epoch']
    model = eval('models.{}.{}(classes_size=test_dataset.classes_size).to(device)'.format(model_dir,model_name))
    model.load_state_dict(best['model_dict'])
    model_result = []
    activate_node = 1 if config.PARAM['num_node']['E']==0 else config.PARAM['num_node']['E']
    for i in range(activate_node):
        test_protocol = init_test_protocol(test_dataset,i)
        result = test(test_loader,model,last_epoch,test_protocol,model_TAG)
        print_result(last_epoch,i,result)
        model_result.append(result)
    return model_result
    
def test(validation_loader,model,epoch,protocol,model_TAG):
    entropy_codec = models.classic.Entropy()
    meter_panel = [Meter_Panel(protocol['metric_names']) for _ in range(protocol['num_iter'])]
    with torch.no_grad():
        model.train(False)
        end = time.time()
        for i, input in enumerate(validation_loader):
            input = collate(input)
            input['img'] = input['img'][input['label']<protocol['num_class']] if(protocol['num_class']>0) else input['img']
            input['label'] = input['label'][input['label']<protocol['num_class']] if(protocol['num_class']>0) else input['label']
            input = dict_to_device(input,device)
            protocol = update_test_protocol(input,i,len(validation_loader),protocol)
            output = model(input,protocol)
            for j in range(protocol['max_num_iter']):
                output[j]['loss'] = torch.mean(output[j]['loss']) if(world_size > 1) else output[j]['loss']
                output[j]['compression']['code'] = entropy_codec.encode(output[j]['compression']['code'],protocol)
                evaluation = meter_panel[j].eval(input,output[j],protocol)
                batch_time = time.time() - end
                meter_panel[j].update(evaluation,len(input['img']))
                meter_panel[j].update({'batch_time':batch_time})
            end = time.time()
        if(tuning_param['compression'] > 0):                                            
            save_img(input['img'],'./output/img/image.png')
            for j in range(len(output)):
                save_img(output[j]['compression']['img'],'./output/img/image_{}_{}_{}.png'.format(model_TAG,epoch,j))
    return meter_panel
    
def init_test_protocol(dataset,activate_node):
    protocol = {}
    protocol['tuning_param'] = config.PARAM['tuning_param'].copy()
    protocol['metric_names'] = config.PARAM['test_metric_names'].copy()
    protocol['loss_mode'] = config.PARAM['loss_mode']
    protocol['node_name'] = {'E':[str(i) for i in range(config.PARAM['num_node']['E'])],'D':[str(i) for i in range(config.PARAM['num_node']['D'])]}
    protocol['num_class'] = config.PARAM['num_class']
    protocol['num_iter'] = config.PARAM['num_iter']
    protocol['activate_node'] = activate_node
    return protocol
    
def collate(input):
    for k in input:
        input[k] = torch.stack(input[k],0)
    return input

def update_test_protocol(input,i,num_batch,protocol):
    if(i == num_batch-1):
        protocol['activate_full'] = True
    else:
        protocol['activate_full'] = False
    if(input['img'].size(1)==1):
        protocol['img_mode'] = 'L'
    elif(input['img'].size(1)==3):
        protocol['img_mode'] = 'RGB'
    else:
        raise ValueError('Wrong number of channel')
    return protocol

def print_result(epoch,activate_node,result):
    for i in range(config.PARAM['num_iter']):
        print('Test Epoch: {}({}_{}){}'.format(epoch,activate_node,i,result[i].summary(['loss']+config.PARAM['test_metric_names'])))
    return
    
if __name__ == "__main__":
    main()