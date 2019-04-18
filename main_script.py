#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Created on Wed Apr 17 14:20:41 2019

@author: Changjia
"""

import numpy as np
import scipy.io
from scipy.sparse.linalg import svds
import os
import h5py
import matplotlib.pyplot as plt
import skimage.morphology
from skimage.morphology import dilation
from skimage.morphology import disk
from scipy import signal
import time
from sklearn.linear_model import LinearRegression
from scipy import stats
from scipy import fftpack

#%%
# opts
opts = {'doCrossVal':False, #cross-validate to optimize regression regularization parameters?
        'contextSize':50,  #65; #number of pixels surrounding the ROI to use as context
        'censorSize':12, #number of pixels surrounding the ROI to censor from the background PCA; roughly the spatial scale of scattered/dendritic neural signals, in pixels.
        'nPC_bg':8, #number of principle components used for background subtraction
        'tau_lp':3, #time window for lowpass filter (seconds); signals slower than this will be ignored
        'tau_pred':1, #time window in seconds for high pass filtering to make predictor for regression
        'sigmas':np.array([1,1.5,2]), #spatial smoothing radius imposed on spatial filter;
        'nIter':5, #number of iterations alternating between estimating temporal and spatial filters.
        'localAlign':False, 
        'globalAlign':True,
        'highPassRegression':False #regress on a high-passed version of the data. Slightly improves detection of spikes, but makes subthreshold unreliable.
       }
output = {}


#%%
dr = '/home/nel/Code/Voltage_imaging/exampledata/403106_3min'
fns = {1:'datasetblock1.mat'}
rois_path = '/home/nel/Code/Voltage_imaging/exampledata/ROIs/403106_3min_rois.mat'
fn_ix = 1
cellN = 0
#%%
print('Loading data batch: ', fns[fn_ix])
arrays = {}
f = h5py.File(dr+'/'+fns[fn_ix],'r')
#for k, v in f.items():
#    arrays[k] = np.array(v)
dataAll = np.array(f.get('data'))
dataAll = dataAll.transpose()

sampleRate = np.array(f.get('sampleRate'))
sampleRate = sampleRate[0][0]
print('sampleRate:',np.int(sampleRate))
opts['windowLength'] = sampleRate*0.02 #window length for spike templates
#%%
# Can not create same disk matrix as matlab, so load the matrix from matlab instead
g = scipy.io.loadmat('/home/nel/Code/Voltage_imaging/disk.mat')
disk_matrix = g['a']
#%%
# Compute global PCs with ROIs masked out 
# To do
#%%
f = scipy.io.loadmat(rois_path)
ROIs = f['roi']

bw = ROIs[:,:,cellN]

# extract relevant region and align
bwexp = dilation(bw,np.ones([opts['contextSize'],opts['contextSize']]), shift_x=True, shift_y=True)
Xinds = np.arange(np.where(np.any(bwexp>0,axis=0)>0)[0][0],np.where(np.any(bwexp>0,axis=0)>0)[0][-1]+1)
Yinds = np.arange(np.where(np.any(bwexp>0,axis=1)>0)[0][0],np.where(np.any(bwexp>0,axis=1)>0)[0][-1]+1)
bw = bw[np.ix_(Yinds,Xinds)]
notbw = 1-dilation(bw, disk_matrix)
#notbw = 1-dilation(bw, disk(opts['censorSize']))

data = dataAll[Yinds[:,np.newaxis],Xinds, :]
bw = (bw>0)
notbw = (notbw>0)



print('processing cell:', cellN)

#%%
# Notice:ROI selection is not the same as matlab
ref = np.median(data[:,:,:500],axis=2)
fig = plt.figure()
plt.subplot(131);plt.imshow(ref);plt.axis('image');plt.xlabel('mean Intensity')
plt.subplot(132);plt.imshow(bw);plt.axis('image');plt.xlabel('initial ROI')
plt.subplot(133);plt.imshow(notbw);plt.axis('image');plt.xlabel('background')
fig.suptitle('ROI selection')
plt.show()

#%%
# local Align
# todo

#%%
output['meanIM'] = np.mean(data, axis=2)
data = np.reshape(data, (-1, data.shape[2]), order='F')

data = np.double(data)
data = np.double(data-np.mean(data,1)[:,np.newaxis])
data = np.double(data-np.mean(data,1)[:,np.newaxis])

#%%
def highpassVideo(video, freq, sampleRate):
    normFreq = freq/(sampleRate/2)
    b, a = signal.butter(3, normFreq, 'high')
    videoFilt = signal.filtfilt(b, a, video, padtype = 'odd', padlen=3*(max(len(b),len(a))-1))
    return videoFilt

#%% remove low frequency components
data_hp = highpassVideo(data, 1/opts['tau_lp'], sampleRate)
data_lp = data-data_hp

if opts['highPassRegression']:
    data_pred = highpassVideo(data, 1/opts['tau_pred'], sampleRate)
else:
    data_pred = data_hp    

#%%
t = np.nanmean(np.double(data_hp[bw.T.ravel(),:]),0)
t = t-np.mean(t)
plt.plot(t[0:200])

#%% remove any variance in trace that can be predicted from the background PCs
Ub, Sb, Vb = svds(np.double(data_hp[notbw.T.ravel(),:]), opts['nPC_bg'])
reg = LinearRegression().fit(Vb.T,t)
reg.coef_
t = t - np.matmul(Vb.T,reg.coef_)

#%% denoiseSpikes
data, windowLength, sampleRate, doPlot, doClip = [-t, opts['windowLength'], sampleRate, True, 100]

#%% highpass filter and threshold
bb, aa = signal.butter(1, 1/(sampleRate/2), 'high') # 1Hz filter
dataHP = signal.filtfilt(bb, aa, data).flatten()

pks = dataHP[signal.find_peaks(dataHP, height=None)[0]]

thresh, _, _, low_spk = getThresh(pks, doClip, 0.25)

locs = signal.find_peaks(dataHP, height=thresh)[0]

#%% peak-traiggered average
window = np.int64(np.arange(-windowLength, windowLength+1, 1))
locs = locs[np.logical_and(locs>(-window[0]), locs<(len(data)-window[-1]))]
PTD = data[(locs[:,np.newaxis]+window)]
PTA = np.mean(PTD, 0)

# matched filter
datafilt = whitenedMatchedFilter(data, locs, window)

#%% Get threshold
#g = scipy.io.loadmat('/home/nel/Code/Voltage_imaging/pks.mat')
#pks = g['pks']
def getThresh(pks, doClip, pnorm=0.5):    
    spread = np.array([pks.min(), pks.max()])
    spread = spread + np.diff(spread) * np.array([-0.05, 0.05])
    low_spk = False
    pts = np.linspace(spread[0], spread[1], 2001)
    kernel = stats.gaussian_kde(pks,bw_method='silverman')
    f = kernel.evaluate(pts)
    xi = pts
    center = np.where(xi>np.median(pks))[0][0]
    #%%
    fmodel = np.concatenate([f[0:center+1], np.flipud(f[0:center])])
    if len(fmodel) < len(f):
        fmodel = np.append(fmodel, np.ones(len(f)-len(fmodel))*min(fmodel))
    else:
        fmodel = fmodel[0:len(f)]
    #%% adjust the model so it doesn't exceed the data:
    csf = np.cumsum(f) / np.sum(f)
    csmodel = np.cumsum(fmodel) / np.max([np.sum(f), np.sum(fmodel)])
    lastpt = np.where(np.logical_and(csf[0:-1]>csmodel[0:-1]+np.spacing(1), csf[1:]<csmodel[1:]))[0]
     
    if not lastpt.size:
        lastpt = center
    else:
        lastpt = lastpt[0]
        
    fmodel[0:lastpt+1] = f[0:lastpt+1]
    fmodel[lastpt:] = np.minimum(fmodel[lastpt:],f[lastpt:])
    
    csf = np.cumsum(f)
    csmodel = np.cumsum(fmodel)
    csf2 = csf[-1] - csf
    csmodel2 = csmodel[-1] - csmodel
    obj = csf2 ** pnorm - csmodel2 ** pnorm
    
    maxind = np.argmax(obj)
    thresh = xi[maxind]
    
    if np.sum(pks>thresh)<30:
        low_spk = True
        print('Very few spikes were detected at the desired sensitivity/specificity tradeoff. Adjusting threshold to take 30 largest spikes')
        thresh = np.percentile(pks, 100*(1-30/len(pks)))
    elif np.sum(pks>thresh)>doClip:
        print('Selecting top',doClip,'spikes for template')
        thresh = np.percentile(pks, 100*(1-doClip/len(pks)))
        
    ix = np.argmin(np.abs(xi-thresh))
    falsePosRate = csmodel2[ix]/csf2[ix]
    detectionRate = (csf2[ix]-csmodel2[ix])/np.max(csf2-csmodel2)

    return thresh, falsePosRate, detectionRate, low_spk

#%% whitened Matched Filter
def whitenedMatchedFilter(data, locs, window):
    N = 2 * len(data) - 1
    censor = np.zeros(len(data))
    censor[locs] = 1
    censor = np.int16(np.convolve(censor.flatten(), np.ones([1, len(window)]).flatten(), 'same'))
    censor = (censor<0.5)
    
    noise = data[censor]
    _,pxx = signal.welch(noise, nperseg=1000, nfft=N)
    Nf2 = np.concatenate([pxx,np.flipud(pxx[:-1])])
    scaling = 1 / np.sqrt(Nf2)
    
    # need to be optimized for fft
    dataScaled = np.real(fftpack.ifft(fftpack.fft(data, N) * scaling))
    
    PTDscaled = dataScaled[(locs[:,np.newaxis]+window)]
    
    PTAscaled = np.mean(PTDscaled, 0)
    
    datafilt = np.convolve(dataScaled, np.flipud(PTAscaled), 'same')
    datafilt = datafilt[:len(data)] 
    
    return datafilt
    
#%%
A = np.array([1,2,3])
signal.find_peaks(A, 6)


#%%
tic = time.time()
elapse = time.time() - tic



