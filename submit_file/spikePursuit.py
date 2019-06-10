#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Created on Fri Apr 19 14:50:09 2019

@author: Changjia Cai based on Matlab code provided by Kaspar and Amrita
"""
import numpy as np
import matplotlib.pyplot as plt
from skimage.morphology import dilation
from skimage.morphology import disk
from sklearn.linear_model import LinearRegression
from scipy import signal
from scipy import stats    
from scipy.sparse.linalg import svds
import pyfftw
import cv2
from caiman.base.movies import movie
import caiman as cm


# %%
def volspike(pars):
    """ Main function for finding spikes of one single neuron with given ROI in
        voltage imaging. Using function denoiseSpikes to find spikes
        of one dimensional signal, using ridge regression to find the
        best spatial filters. Do these two steps iteratively to find
        best spike time.

        Args:
            pars: list
                two variables including args and cellN

                args: dictionary

                    fnames: str
                        name of the memory map file

                    fr: int
                        sample rate of the video

                    ROIs: 3-d array
                        all region of interests

                    doCrossVal: boolean
                        whether to use cross validation to optimize regression regularization parameters

                    doGlobalSubtract: boolean
                        whether to subtract the signal which can be predicted by the entire video

                    contextSize: int
                        number of pixels surrounding the ROI to use as context

                    censorSize: int
                        number of pixels surrounding the ROI to censor from the background PCA; roughly
                        the spatial scale of scattered/dendritic neural signals, in pixels

                    nPC_bg: int
                        number of principle components used for background subtraction

                    tau_lp: int
                        time window for lowpass filter (seconds); signals slower than this will be ignored

                    tau_pred: int
                        time window in seconds for high pass filtering to make predictor for regression

                    sigmas: 1-d array
                        spatial smoothing radius imposed on spatial filter

                    nIter: int
                        number of iterations alternating between estimating temporal and spatial filters

                    localAlign: boolean

                    globalAlign: boolean

                    highPassRegression: boolean
                        whether to regress on a high-passed version of the data. Slightly improves detection of spikes,
                        but makes subthreshold unreliable

                cellN: int
                    number of cell processing
        Returns:
            output: a dictionary
                output including spike times, spatial filters etc
    """
    args = pars[0]
    cellN = pars[1]
    print('Now processing cell number {0}'.format(cellN))
    fnames = args['fnames']
    sampleRate = args['fr']
    bw = args['ROIs'][cellN]
    doCrossVal = args['doCrossVal']
    doGlobalSubtract = args['doGlobalSubtract']
    contextSize = args['contextSize']
    censorSize = args['censorSize']
    nPC_bg = args['nPC_bg']
    tau_lp = args['tau_lp']
    tau_pred = args['tau_pred']
    sigmas = args['sigmas']
    nIter = args['nIter']
    localAlign = args['localAlign']
    globalAlign = args['globalAlign']
    highPassRegression = args['highPassRegression']
    windowLength = sampleRate * 0.02 # window length for spike templates
    output = {}
    output['rawROI'] = {}

    Yr, dims, T = cm.load_memmap(fnames)
    if bw.shape == dims:
        images = np.reshape(Yr.T, [T] + list(dims), order='F')
    elif bw.shape == dims[::-1]:
        images = np.reshape(Yr.T, [T] + list(dims), order='F').transpose([0, 2, 1])
    else:
        print('size of ROI and video does not accrod')

    # extract relevant region and align
    bwexp = dilation(bw, np.ones([contextSize, contextSize]), shift_x=True, shift_y=True)
    Xinds = np.where(np.any(bwexp > 0, axis=1) > 0)[0]
    Yinds = np.where(np.any(bwexp > 0, axis=0) > 0)[0]
    bw = bw[Xinds[0]:Xinds[-1] + 1, Yinds[0]:Yinds[-1] + 1]
    notbw = 1 - dilation(bw, disk(censorSize))
    data = np.array(images[:, Xinds[0]:Xinds[-1] + 1, Yinds[0]:Yinds[-1] + 1])
    bw = (bw > 0)
    notbw = (notbw > 0)
    ref = np.median(data[:500, :, :], axis=0)

    # visualize ROI
    # fig = plt.figure()
    # plt.subplot(131);plt.imshow(ref);plt.axis('image');plt.xlabel('mean Intensity')
    # plt.subplot(132);plt.imshow(bw);plt.axis('image');plt.xlabel('initial ROI')
    # plt.subplot(133);plt.imshow(notbw);plt.axis('image');plt.xlabel('background')
    # fig.suptitle('ROI selection')
    # plt.show()

    output['meanIM'] = np.mean(data, axis=0)
    data = np.reshape(data, (data.shape[0], -1))
    data = data - np.mean(data, 0)
    data = data - np.mean(data, 0)

    # remove low frequency components
    data_hp = highpassVideo(data.T, 1 / tau_lp, sampleRate).T
    data_lp = data - data_hp
    data_pred = np.empty_like(data_hp)
    if highPassRegression:
        data_pred[:] = highpassVideo(data, 1 / tau_pred, sampleRate)
    else:
        data_pred[:] = data_hp

    # initial trace
    t = np.nanmean(data_hp[:, bw.ravel()], 1)
    t = t - np.mean(t)

    # remove any variance in trace that can be predicted from the background principal components
    Ub, Sb, Vb = svds(data_hp[:, notbw.ravel()], nPC_bg)
    reg = LinearRegression(fit_intercept=False).fit(Ub, t)
    t = np.double(t - np.matmul(Ub, reg.coef_))

    # find out spikes of initial trace
    Xspikes, spikeTimes, guessData, output['rawROI']['falsePosRate'], output['rawROI']['detectionRate'], \
    output['rawROI']['templates'], low_spk = denoiseSpikes(-t, windowLength, sampleRate, False, 100)

    Xspikes = -Xspikes
    output['rawROI']['X'] = t.copy()
    output['rawROI']['Xspikes'] = Xspikes.copy()
    output['rawROI']['spikeTimes'] = spikeTimes.copy()
    output['rawROI']['spatialFilter'] = bw.copy()
    output['rawROI']['X'] = output['rawROI']['X'] * np.mean(t[output['rawROI']['spikeTimes']]) / np.mean(
        output['rawROI']['X'][output['rawROI']['spikeTimes']])  # correct shrinkage
    output['num_spikes'] = [spikeTimes.shape[0]]
    templates = output['rawROI']['templates']
    selectSpikes = np.zeros(Xspikes.shape)
    selectSpikes[spikeTimes] = 1
    sgn = np.mean(Xspikes[selectSpikes > 0])
    noise = np.std(Xspikes[selectSpikes == 0])
    snr = sgn / noise

    # prebuild the regression matrix
    # generate a predictor for ridge regression
    pred = np.empty_like(data_pred)
    pred[:] = data_pred
    pred = np.hstack((np.ones((data_pred.shape[0], 1), dtype=np.single), np.reshape
    (movie.gaussian_blur_2D(np.reshape(pred,
                                       (data_hp.shape[0], ref.shape[0], ref.shape[1])),
                            kernel_size_x=7, kernel_size_y=7, kernel_std_x=1.5,
                            kernel_std_y=1.5, borderType=cv2.BORDER_REPLICATE), data_hp.shape)))

    # Cross-validation of regularized regression parameters
    lambdamax = np.single(np.linalg.norm(pred[:, 1:], ord='fro') ** 2)
    lambdas = lambdamax * np.logspace(-4, -2, 3)
    I0 = np.eye(pred.shape[1], dtype=np.single)
    I0[0, 0] = 0

    if doCrossVal:
        # need to add
        print('doing cross validation')
    else:
        s_max = 1
        l_max = 2
        lambd = lambdas[l_max]
        sigma = sigmas[s_max]
        lambda_ix = l_max

    selectPred = np.ones(data_hp.shape[0])
    if highPassRegression:
        selectPred[:np.int16(sampleRate / 2 + 1)] = 0
        selectPred[-1 - np.int16(sampleRate / 2):] = 0
    sigma = sigmas[s_max]

    pred = np.empty_like(data_pred)
    pred[:] = data_pred
    pred = np.hstack((np.ones((data_pred.shape[0], 1), dtype=np.single), np.reshape
    (movie.gaussian_blur_2D(np.reshape(pred,
                                       (data_pred.shape[0], ref.shape[0], ref.shape[1])),
                            kernel_size_x=np.int(2 * np.ceil(2 * sigma) + 1),
                            kernel_size_y=np.int(2 * np.ceil(2 * sigma) + 1),
                            kernel_std_x=sigma, kernel_std_y=sigma,
                            borderType=cv2.BORDER_REPLICATE), data_pred.shape)))

    recon = np.empty_like(data_hp)
    recon[:] = data_hp
    recon = np.hstack((np.ones((data_hp.shape[0], 1), dtype=np.single), np.reshape
    (movie.gaussian_blur_2D(np.reshape(recon,
                                       (data_hp.shape[0], ref.shape[0], ref.shape[1])),
                            kernel_size_x=np.int(2 * np.ceil(2 * sigma) + 1),
                            kernel_size_y=np.int(2 * np.ceil(2 * sigma) + 1),
                            kernel_std_x=sigma, kernel_std_y=sigma,
                            borderType=cv2.BORDER_REPLICATE), data_hp.shape)))

    temp = np.linalg.inv(
        np.matmul(np.transpose(pred[selectPred > 0, :]), pred[selectPred > 0, :]) + lambdas[l_max] * I0)
    kk = np.matmul(temp, np.transpose(pred[selectPred > 0, :]))

    # Identify spatial filters with regularized regression
    for iteration in range(nIter):
        doPlot = False
        if iteration == nIter - 1:
            doPlot = True

        # print('Identifying spatial filters')
        # print(iteration)

        gD = np.single(guessData[selectPred > 0])
        select = (gD != 0)
        weights = np.matmul(kk[:, select], gD[select])

        X = np.matmul(recon, weights)
        X = X - np.mean(X)

        spatialFilter = np.empty_like(weights)
        spatialFilter[:] = weights
        spatialFilter = movie.gaussian_blur_2D(np.reshape(spatialFilter[1:],
                                                          ref.shape, order='C')[np.newaxis, :, :],
                                               kernel_size_x=np.int(2 * np.ceil(2 * sigma) + 1),
                                               kernel_size_y=np.int(2 * np.ceil(2 * sigma) + 1),
                                               kernel_std_x=sigma, kernel_std_y=sigma,
                                               borderType=cv2.BORDER_REPLICATE)[0]

        if iteration < nIter - 1:
            b = LinearRegression(fit_intercept=False).fit(Ub, X).coef_
            if doPlot:
                plt.figure()
                plt.plot(X)
                plt.plot(np.matmul(Ub, b))
                plt.title('Denoised trace vs background')
                plt.show()
            X = X - np.matmul(Ub, b)
        else:
            b = LinearRegression(fit_intercept=False).fit(Ub, X).coef_
            X = X - np.matmul(Ub, b)
            if doGlobalSubtract:
                print('do global subtract')
            # need to add

        # correct shrinkage
        X = np.double(X * np.mean(t[spikeTimes]) / np.mean(X[spikeTimes]))

        # generate the new trace and the new denoised trace
        Xspikes, spikeTimes, guessData, falsePosRate, detectionRate, templates, _ = denoiseSpikes(-X,
                                                                                                  windowLength,
                                                                                                  sampleRate, doPlot)

        selectSpikes = np.zeros(Xspikes.shape)
        selectSpikes[spikeTimes] = 1
        sgn = np.mean(Xspikes[selectSpikes > 0])
        noise = np.std(Xspikes[selectSpikes == 0])
        snr = sgn / noise

        output['num_spikes'].append(spikeTimes.shape[0])

        # ensure that the maximum of the spatial filter is within the ROI
    matrix = np.matmul(np.transpose(pred[:, 1:]), -guessData)
    sigmax = np.sqrt(np.sum(np.multiply(pred[:, 1:], pred[:, 1:]), axis=0))
    sigmay = np.sqrt(np.dot(guessData, guessData))
    IMcorr = matrix / sigmax / sigmay
    maxCorrInROI = np.max(IMcorr[bw.ravel()])
    if np.any(IMcorr[notbw.ravel()] > maxCorrInROI):
        output['passedLocalityTest'] = False
    else:
        output['passedLocalityTest'] = True

    # compute SNR
    selectSpikes = np.zeros(Xspikes.shape)
    selectSpikes[spikeTimes] = 1
    sgn = np.mean(Xspikes[selectSpikes > 0])
    noise = np.std(Xspikes[selectSpikes == 0])
    snr = sgn / noise
    output['snr'] = snr

    # output
    output['y'] = X
    output['yFilt'] = -Xspikes
    output['ROI'] = np.transpose(np.vstack((Xinds[[0, -1]], Yinds[[0, -1]])))
    output['ROIbw'] = bw
    output['spatialFilter'] = spatialFilter
    output['falsePosRate'] = falsePosRate
    output['detectionRate'] = detectionRate
    output['templates'] = templates
    output['spikeTimes'] = spikeTimes
    output['F0'] = np.nanmean(data_lp[:, bw.flatten()] + output['meanIM'][bw][np.newaxis, :], 1)
    output['dFF'] = X / output['F0']
    output['rawROI']['dFF'] = output['rawROI']['X'] / output['F0']
    output['bg_pc'] = Ub  # background components
    output['low_spk'] = low_spk
    output['weights'] = weights
    output['cellN'] = cellN

    return output


def denoiseSpikes(data, windowLength, sampleRate=400, doPlot=True, doClip=150):
    """ Function for finding spikes and the temporal filter given one dimensional signals.
        Use function whitenedMatchedFilter to denoise spikes. Function getThresh
        helps to find the best threshold given height of spikes.

    Args:
        data: 1-D array
            one dimensional signal

        windowLength: int
            length of window size for temporal filter

        sampleRate: int, default 400
            number of samples per second in the video

        doPlot: boolean, default:True
            if Ture, will plot trace of signals and spiketimes, peak triggered
            average, histogram of heights,

        doClip: int, default:150
            maximum number of spikes accepted

    Returns:
        datafilt: 1-D array
            signals after whitened matched filter

        spikeTimes: 1-D array
            record of time of spikes

        guessData: 1-D array
            recovery of original signals

        falsePosRate: float
            possibility of misclassify noise as real spikes

        detectionRate: float
            possibility of real spikes being detected

        templates: 1-D array
            temporal filter which is the peak triggered average

        low_spk: boolean
            true if number of spikes is smaller than 30
    """

    # highpass filter and threshold
    bb, aa = signal.butter(1, 1 / (sampleRate / 2), 'high')  # 1Hz filter
    dataHP = signal.filtfilt(bb, aa, data, padtype='odd', padlen=3 * (max(len(bb), len(aa)) - 1)).flatten()

    pks = dataHP[signal.find_peaks(dataHP, height=None)[0]]

    thresh, _, _, low_spk = getThresh(pks, doClip, 0.25)

    locs = signal.find_peaks(dataHP, height=thresh)[0]

    # peak-traiggered average
    window = np.int64(np.arange(-windowLength, windowLength + 1, 1))

    locs = locs[np.logical_and(locs > (-window[0]), locs < (len(data) - window[-1]))]
    PTD = data[(locs[:, np.newaxis] + window)]
    PTA = np.mean(PTD, 0)

    # matched filter
    datafilt = whitenedMatchedFilter(data, locs, window)

    # spikes detected after filter
    pks2 = datafilt[signal.find_peaks(datafilt, height=None)[0]]

    thresh2, falsePosRate, detectionRate, _ = getThresh(pks2, doClip=0, pnorm=0.5)  # doClip=0 means no clipping
    spikeTimes = signal.find_peaks(datafilt, height=thresh2)[0]

    guessData = np.zeros(data.shape)
    guessData[spikeTimes] = 1
    guessData = np.convolve(guessData, PTA, 'same')

    # filtering shrinks the data;
    # rescale so that the mean value at the peaks is same as in the input
    datafilt = datafilt * np.mean(data[spikeTimes]) / np.mean(datafilt[spikeTimes])

    # output templates
    templates = PTA

    # plot three graphs
    if doPlot:
        plt.figure()
        plt.subplot(211)
        plt.hist(pks, 500)
        plt.axvline(x=thresh, c='r')
        plt.title('raw data')
        plt.subplot(212)
        plt.hist(pks2, 500)
        plt.axvline(x=thresh2, c='r')
        plt.title('after matched filter')
        plt.tight_layout()
        plt.show()

        plt.figure()
        plt.plot(np.transpose(PTD), c=[0.5, 0.5, 0.5])
        plt.plot(PTA, c='black', linewidth=2)
        plt.title('Peak-triggered average')
        plt.show()

        plt.figure()
        plt.subplot(211)
        plt.plot(data)
        plt.plot(locs, np.max(datafilt) * 1.1 * np.ones(locs.shape), color='r', marker='o', fillstyle='none',
                 linestyle='none')
        plt.plot(spikeTimes, np.max(datafilt) * 1 * np.ones(spikeTimes.shape), color='g', marker='o', fillstyle='none',
                 linestyle='none')
        plt.subplot(212)
        plt.plot(datafilt)
        plt.plot(locs, np.max(datafilt) * 1.1 * np.ones(locs.shape), color='r', marker='o', fillstyle='none',
                 linestyle='none')
        plt.plot(spikeTimes, np.max(datafilt) * 1 * np.ones(spikeTimes.shape), color='g', marker='o', fillstyle='none',
                 linestyle='none')
        plt.show()

    return datafilt, spikeTimes, guessData, falsePosRate, detectionRate, templates, low_spk


def getThresh(pks, doClip, pnorm=0.5):
    """ Function for deciding threshold given heights of all peaks.

    Args:
        pks: 1-D array
            height of all peaks

        doClip: int

        pnorm: float, between 0 and 1, default is 0.5
            a variable deciding the amount of spikes chosen

    Returns:
        thresh: float
            threshold for choosing spikes

        falsePosRate: float
            possibility of misclassify noise as real spikes

        detectionRate: float
            possibility of real spikes being detected

        low_spk: boolean
            true if number of spikes is smaller than 30
    """
    # find median of the kernel density estimation of peak heights
    spread = np.array([pks.min(), pks.max()])
    spread = spread + np.diff(spread) * np.array([-0.05, 0.05])
    low_spk = False
    pts = np.linspace(spread[0], spread[1], 2001)
    kde = stats.gaussian_kde(pks)
    f = kde(pts)    
    xi = pts
    center = np.where(xi > np.median(pks))[0][0]

    fmodel = np.concatenate([f[0:center + 1], np.flipud(f[0:center])])
    if len(fmodel) < len(f):
        fmodel = np.append(fmodel, np.ones(len(f) - len(fmodel)) * min(fmodel))
    else:
        fmodel = fmodel[0:len(f)]

    # adjust the model so it doesn't exceed the data:
    csf = np.cumsum(f) / np.sum(f)
    csmodel = np.cumsum(fmodel) / np.max([np.sum(f), np.sum(fmodel)])
    lastpt = np.where(np.logical_and(csf[0:-1] > csmodel[0:-1] + np.spacing(1), csf[1:] < csmodel[1:]))[0]
    if not lastpt.size:
        lastpt = center
    else:
        lastpt = lastpt[0]
    fmodel[0:lastpt + 1] = f[0:lastpt + 1]
    fmodel[lastpt:] = np.minimum(fmodel[lastpt:], f[lastpt:])

    # find threshold
    csf = np.cumsum(f)
    csmodel = np.cumsum(fmodel)
    csf2 = csf[-1] - csf
    csmodel2 = csmodel[-1] - csmodel
    obj = csf2 ** pnorm - csmodel2 ** pnorm
    maxind = np.argmax(obj)
    thresh = xi[maxind]

    if np.sum(pks > thresh) < 30:
        low_spk = True
        print(
            'Very few spikes were detected at the desired sensitivity/specificity tradeoff. Adjusting threshold to take 30 largest spikes')
        thresh = np.percentile(pks, 100 * (1 - 30 / len(pks)))
    elif ((np.sum(pks > thresh) > doClip) & (doClip > 0)):
        print('Selecting top', doClip, 'spikes for template')
        thresh = np.percentile(pks, 100 * (1 - doClip / len(pks)))

    ix = np.argmin(np.abs(xi - thresh))
    falsePosRate = csmodel2[ix] / csf2[ix]
    detectionRate = (csf2[ix] - csmodel2[ix]) / np.max(csf2 - csmodel2)
    return thresh, falsePosRate, detectionRate, low_spk


def whitenedMatchedFilter(data, locs, window):
    """
    Function for using whitened matched filter to the original signal for better
    SNR. Use welch method to approximate the spectral density of the signal.
    Rescale the signal in frequency domain.
    """
    N = np.ceil(np.log2(len(data)))
    censor = np.zeros(len(data))
    censor[locs] = 1
    censor = np.int16(np.convolve(censor.flatten(), np.ones([1, len(window)]).flatten(), 'same'))
    censor = (censor < 0.5)
    noise = data[censor]

    _, pxx = signal.welch(noise, fs=2 * np.pi, window=signal.get_window('hamming', 1000), nfft=2 ** N, detrend=False,
                          nperseg=1000)
    Nf2 = np.concatenate([pxx, np.flipud(pxx[1:-1])])
    scaling = 1 / np.sqrt(Nf2)

    # Use pyfftw for fast fourier transform
    a = pyfftw.empty_aligned(data.shape[0], dtype='float64')
    a[:] = data
    dataScaled = np.real(pyfftw.interfaces.scipy_fftpack.ifft(pyfftw.interfaces.scipy_fftpack.fft(a, 2 ** N) * scaling))
    PTDscaled = dataScaled[(locs[:, np.newaxis] + window)]
    PTAscaled = np.mean(PTDscaled, 0)
    datafilt = np.convolve(dataScaled, np.flipud(PTAscaled), 'same')
    datafilt = datafilt[:len(data)]
    return datafilt


def highpassVideo(video, freq, sampleRate):
    """
    Function for passing signals with frequency higher than freq
    """
    normFreq = freq / (sampleRate / 2)
    b, a = signal.butter(3, normFreq, 'high')
    videoFilt = np.single(signal.filtfilt(b, a, video, padtype='odd', padlen=3 * (max(len(b), len(a)) - 1)))
    return videoFilt








