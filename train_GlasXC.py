from datetime import datetime
from functools import partial
from matplotlib import pyplot as plt
from matplotlib import gridspec as gs
from argparse import ArgumentParser, ArgumentDefaultsHelpFormatter

from XMC.GlasXC import GlasXC
from XMC.loaders import LibSVMLoader
from XMC.metrics import precision_at_k, ndcg_score_at_k

import math
import os
import yaml
import torch
import numpy as np
import torch.nn.functional as F


def weights_init(mdl, scheme):
    """
    Function to initialize weights

    Args:
        mdl : Module whose weights are going to modified
        scheme : Scheme to use for weight initialization
    """
    if isinstance(mdl, torch.nn.Linear):
        func = getattr(torch.nn.init, scheme + '_')  # without underscore is deprecated
        func(mdl.weight)


TIME_STAMP = datetime.utcnow().isoformat()

parser = ArgumentParser(formatter_class=ArgumentDefaultsHelpFormatter)

# data argument
parser.add_argument('--data_root', type=str, required=True,
                    help="""Root folder for dataset.
                            Note that the root folder should contain files either ending with
                            test / train""")
parser.add_argument('--dataset_info', type=str, required=True,
                    help='Dataset information in YAML format')

# architecture arguments
parser.add_argument('--input_encoder_cfg', type=str, required=True,
                    help='Input Encoder architecture configuration in YAML format')
parser.add_argument('--input_decoder_cfg', type=str, required=True,
                    help='Input Decoder architecture configuration in YAML format')
parser.add_argument('--output_encoder_cfg', type=str, required=True,
                    help='Output Encoder architecture configuration in YAML format')
parser.add_argument('--output_decoder_cfg', type=str, required=True,
                    help='Output Decoder architecture configuration in YAML format')
parser.add_argument('--regressor_cfg', type=str, required=True,
                    help='Regressor architecture configuration in YAML format')
parser.add_argument('--init_scheme', type=str, default='default',
                    choices=['xavier_uniform', 'kaiming_uniform', 'default'])

# training configuration arguments
parser.add_argument('--device', type=str, default='cpu',
                    help='PyTorch device string <device_name>:<device_id>')
parser.add_argument('--seed', type=int, default=None,
                    help='Manually set the seed for the experiments for reproducibility')
parser.add_argument('--batch_size', type=int, default=64,
                    help='Batch size for training')
parser.add_argument('--epochs', type=int, default=10,
                    help='Number of epochs to train the autoencoder for')
parser.add_argument('--interval', type=int, default=-1,
                    help='Interval between two status updates on training')
parser.add_argument('--input_ae_loss_weight', type=float, default=1.,
                    help='Weight to give the input autoencoder loss in the entire loss')
parser.add_argument('--output_ae_loss_weight', type=float, default=1.,
                    help='Weight to give the output autoencoder loss in the entire loss')
parser.add_argument('--plot', action='store_true',
                    help='Option to plot the loss variation over iterations')

# optimizer arguments
parser.add_argument('--optimizer_cfg', type=str, required=True,
                    help='Optimizer configuration in YAML format for GlasXC model')

# post training arguments
parser.add_argument('--save_model', type=str, default=None,
                    choices=['all', 'inputAE', 'outputAE', 'regressor'], nargs='+',
                    help='Options to save the model partially or completely')
parser.add_argument('--k', type=int, default=5,
                    help='k for Precision at k and NDCG at k')

# parse the arguments
args = parser.parse_args()

# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ CUDA Capability ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

cur_device = torch.device(args.device)
USE_CUDA = cur_device.type == 'cuda'
if USE_CUDA and not torch.cuda.is_available():
    raise ValueError("You can't use CUDA if you don't have CUDA")

# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ Reproducibility ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

if args.seed is not None:
    torch.manual_seed(args.seed)
    if USE_CUDA:
        torch.cuda.manual_seed(args.seed)

# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ Model initialization ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

input_enc_cfg = yaml.load(open(args.input_encoder_cfg))
input_dec_cfg = yaml.load(open(args.input_decoder_cfg))
output_enc_cfg = yaml.load(open(args.output_encoder_cfg))
output_dec_cfg = yaml.load(open(args.output_decoder_cfg))
regress_cfg = yaml.load(open(args.regressor_cfg))

Glas_XC = GlasXC(input_enc_cfg, input_dec_cfg, output_enc_cfg, output_dec_cfg, regress_cfg)
if args.init_scheme != 'default':
    Glas_XC.apply(partial(weights_init, scheme=args.init_scheme))
Glas_XC = Glas_XC.to(cur_device)

# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ Optimizer initialization ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

opt_options = yaml.load(open(args.optimizer_cfg))
optimizer = getattr(torch.optim, opt_options['name'])(Glas_XC.parameters(),
                                                      **opt_options['args'])

# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ Dataloader initialization ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

loader_kwargs = {}
if USE_CUDA:
    loader_kwargs = {'num_workers': 1, 'pin_memory': True}

dset_opts = yaml.load(open(args.dataset_info))
USE_TEST_DSET = 'test_filename' in dset_opts.keys()

train_file = os.path.join(args.data_root, dset_opts['train_filename'])
train_loader = LibSVMLoader(train_file, dset_opts['train_opts'])
len_loader = len(train_loader)
train_data_loader = torch.utils.data.DataLoader(train_loader, batch_size=args.batch_size,
                                                shuffle=True, **loader_kwargs)

if USE_TEST_DSET:
    test_file = os.path.join(args.data_root, dset_opts['test_filename'])
    test_loader = LibSVMLoader(test_file, dset_opts['test_opts'])
    test_data_loader = torch.utils.data.DataLoader(test_loader, batch_size=1000,
                                                   shuffle=False)

# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ Train your model ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

all_iters = 0
ALPHA_INPUT = args.input_ae_loss_weight
ALPHA_OUTPUT = args.output_ae_loss_weight
K = args.k
INP_REC_LOSS = []
OTP_REC_LOSS = []
CLASS_LOSS = []
AVG_P_AT_K = []
AVG_NDCG_AT_K = []
LAMBDA = 10
mean = 0.5
div = 1/math.pow(2456,2)
#epsilon = 10e-5                # Use only for Eurlex because Z becomes singular

for epoch in range(args.epochs):
    cur_no = 0
    for x, y in iter(train_data_loader):
        x = x.to(device=cur_device, dtype=torch.float)
        y = y.to(device=cur_device, dtype=torch.float)
        cur_no += x.size(0)

        optimizer.zero_grad()
        inp_ae_fp, out_ae_fp, reg_fp = Glas_XC.forward(x, y)

	# Build GLAS Regularizer
	
	# Sampling the Label Matrix - Start

	valid_idx = torch.nonzero(y)  # get all the non zeros
	t = torch.flatten(valid_idx)  # flatten the tuple into a list tensor	
	t1 = t[1::2].unique()  	      # index of non zero column is at odd postion elements, get the unique column indices
	t2 = t1.numpy()		      # convert tensor to numpy array for uniform random sampling, no counterpart function in Pytorch
	t3 = numpy.random.choice(t2, batch_size, replace=False)  # random sampling values of array without replacement
	indices = torch.from_numpy(y)    			 # Final Sampled label indexs
	y_sampled = torch.index_select(y, 1, indices)  #indexes the input tensor along column using the entries in indices
	
	# Sampling the Label matrix per batch done!
	
        v  = Glas_XC.encode_output(y)    # Label Embedding Matrix for mini-batch
        V  = torch.mm(v.t(), v)               # co-occurence in the latent/embedded space
        A  = torch.mm(y.t(), y)  			  # models co-occurence of labels
        Z  = torch.diag(A)  #+ epsilon        # returns the diagoan in vector form
        Z  = torch.diag(Z)       			  # creates the diagonal from the vector
        AZ = torch.add(torch.mm(A, torch.inverse(Z)), torch.mm(torch.inverse(Z), A))
        M  = mean*AZ            			  # Mean of conditional frequencies of label
        g  = torch.sub(V, M)    			  	
        gl = torch.norm(g, p='fro')
        loss_glas = div * gl*gl                  # final loss of glas regularizer 



       # print("The size of encoded output label is")
       # print(V.size())


        # This is a custom that we will be using to backprop. It has three components:
        # 1. Reconstruction error of input
        # 2. Reconstruction error of output
        # 3. Classification (Binary cross entropy) of input-output
        # The first two are weighted using ALPHA_INPUT and ALPHA_OUTPUT.

        #loss_class = F.multilabel_margin_loss(reg_fp, y)
        loss_class = F.binary_cross_entropy(reg_fp, y) + LAMBDA * loss_glas
        net_loss = loss_class
        net_loss.backward()
        optimizer.step()
        all_iters += 1
        if all_iters % args.interval == 0:
            print("{} / {} :: {} / {} - CLASS_LOSS : {}"
                  .format(epoch, args.epochs, cur_no, len_loader,round(loss_class.item(), 5)))
        
        CLASS_LOSS.append(loss_class.item())

    pred_y = []
    actual_y = []
    for x, y in iter(train_data_loader):
        x = x.to(device=cur_device, dtype=torch.float)

        pred_y.append(Glas_XC.predict(x).detach().cpu().numpy())
        actual_y.append(y.numpy())

    pred_y = np.vstack(pred_y)
    actual_y = np.vstack(actual_y)
    p_at_k = [precision_at_k(actual_y[i], pred_y[i], K) for i in range(len(pred_y))]
    ndcg_at_k = [ndcg_score_at_k(actual_y[i], pred_y[i], K) for i in range(len(pred_y))]
    print("{0} / {1} :: Precision at {2}: {3}\tNDCG at {2}: {4}"
          .format(epoch, args.epochs, K, np.mean(p_at_k), np.mean(ndcg_at_k)))
    AVG_P_AT_K.append(np.mean(p_at_k))
    AVG_NDCG_AT_K.append(np.mean(ndcg_at_k))

# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ Plot graphs ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
if args.plot:
    fig = plt.figure(figsize=(9, 18))
    gridspec = gs.GridSpec(4, 6, figure=fig)
    gridspec.tight_layout(fig)
    ax1 = plt.subplot(gridspec[0, :2])
    ax2 = plt.subplot(gridspec[0, 2:4])
    ax3 = plt.subplot(gridspec[0, 4:])
    #ax4 = plt.subplot(gridspec[1:3, 1:5])
    #ax5 = plt.subplot(gridspec[3, :3])
    #ax6 = plt.subplot(gridspec[3, 3:])

    
    ax1.plot(list(range(1, all_iters + 1)), CLASS_LOSS, 'b', linewidth=2.0)
    ax1.set_title('Classification loss')
    ax2.plot(list(range(1, args.epochs + 1)), AVG_P_AT_K, 'g', linewidth=2.0)
    ax2.set_title('Average Precision at {} (over all datapoints) with epochs'.format(K))
    ax3.plot(list(range(1, args.epochs + 1)), AVG_NDCG_AT_K, 'b', linewidth=2.0)
    ax3.set_title('Average NDCG at {} (over all datapoints) with epochs'.format(K))
    plt.show()

# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ Save your model ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

if args.save_model is not None:
    if 'inputAE' in args.save_model or 'all' in args.save_model:
        torch.save(Glas_XC.input_encoder.to('cpu'),
                   'trained_input_encoder_{}.pt'.format(TIME_STAMP))
        torch.save(Glas_XC.input_decoder.to('cpu'),
                   'trained_input_decoder_{}.pt'.format(TIME_STAMP))

    if 'outputAE' in args.save_model or 'all' in args.save_model:
        torch.save(Glas_XC.output_encoder.to('cpu'),
                   'trained_output_encoder_{}.pt'.format(TIME_STAMP))
        torch.save(Glas_XC.output_decoder.to('cpu'),
                   'trained_output_decoder_{}.pt'.format(TIME_STAMP))

    if 'regressor' in args.save_model or 'all' in args.save_model:
        torch.save(Glas_XC.regressor.to('cpu'),
                   'trained_regressor_{}.pt'.format(TIME_STAMP))

# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ Prediction on test ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

if USE_TEST_DSET:
    print("Test set characteristics")
    pred_y = []
    actual_y = []
    for x, y in iter(test_data_loader):
        x = x.to(device=cur_device, dtype=torch.float)

        pred_y.append(Glas_XC.predict(x).detach().cpu().numpy())
        actual_y.append(y.numpy())

    pred_y = np.vstack(pred_y)
    actual_y = np.vstack(actual_y)
    p_at_k = [precision_at_k(actual_y[i], pred_y[i], K) for i in range(len(pred_y))]
    ndcg_at_k = [ndcg_score_at_k(actual_y[i], pred_y[i], K) for i in range(len(pred_y))]
    print("Precision at {2}: {3}\tNDCG at {2}: {4}"
          .format(epoch, args.epochs, K, np.mean(p_at_k), np.mean(ndcg_at_k)))
