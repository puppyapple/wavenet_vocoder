from concurrent.futures import ProcessPoolExecutor
from functools import partial
import numpy as np
import os
import audio

from nnmnkwii import preprocessing as P
from hparams import hparams
from os.path import exists, basename, splitext
import librosa
from glob import glob
from os.path import join

from wavenet_vocoder.util import is_mulaw_quantize, is_mulaw, is_raw


def build_from_path(in_dir, out_dir, local_mel_dir=None, num_workers=1, tqdm=lambda x: x):
    executor = ProcessPoolExecutor(max_workers=num_workers)
    futures = []
    index = 1
    src_files = sorted(glob(join(in_dir, "*.wav")))
    # added by wuzijun
    local_mel_files = [join(local_mel_dir, splitext(basename(path))[0] + ".npy") for path in src_files] if local_mel_dir is not None else [None] * len(src_files)
    src_couples = zip(src_files, local_mel_files)
    for wav_path, mel_path in src_couples:
        futures.append(executor.submit(
            partial(_process_utterance, out_dir, index, wav_path, mel_path, "dummy")))
        index += 1
    return [future.result() for future in tqdm(futures)]


def _process_utterance(out_dir, index, wav_path, mel_path, text):
    # Load the audio to a numpy array:
    wav = audio.load_wav(wav_path)

    # Trim begin/end silences
    # NOTE: the threshold was chosen for clean signals
    wav, _ = librosa.effects.trim(wav, top_db=60, frame_length=2400, hop_length=600)

    if hparams.highpass_cutoff > 0.0:
        wav = audio.low_cut_filter(wav, hparams.sample_rate, hparams.highpass_cutoff)

    # Mu-law quantize
    if is_mulaw_quantize(hparams.input_type):
        # Trim silences in mul-aw quantized domain
        silence_threshold = 0
        if silence_threshold > 0:
            # [0, quantize_channels)
            out = P.mulaw_quantize(wav, hparams.quantize_channels - 1)
            start, end = audio.start_and_end_indices(out, silence_threshold)
            wav = wav[start:end]
        constant_values = P.mulaw_quantize(0, hparams.quantize_channels - 1)
        out_dtype = np.int16
    elif is_mulaw(hparams.input_type):
        # [-1, 1]
        constant_values = P.mulaw(0.0, hparams.quantize_channels - 1)
        out_dtype = np.float32
    else:
        # [-1, 1]
        constant_values = 0.0
        out_dtype = np.float32

    # Compute a mel-scale spectrogram from the trimmed wav:
    # (N, D)
    mel_spectrogram = audio.logmelspectrogram(wav).astype(np.float32).T if mel_path is None else np.load(mel_path).T

    if hparams.global_gain_scale > 0:
        wav *= hparams.global_gain_scale

    # Time domain preprocessing
    if hparams.preprocess is not None and hparams.preprocess not in ["", "none"]:
        f = getattr(audio, hparams.preprocess)
        wav = f(wav)

    # Clip
    if np.abs(wav).max() > 1.0:
        print("""Warning: abs max value exceeds 1.0: {}""".format(np.abs(wav).max()))
        # ignore this sample
        return ("dummy", "dummy", -1, "dummy")

    wav = np.clip(wav, -1.0, 1.0)

    # Set waveform target (out)
    if is_mulaw_quantize(hparams.input_type):
        out = P.mulaw_quantize(wav, hparams.quantize_channels - 1)
    elif is_mulaw(hparams.input_type):
        out = P.mulaw(wav, hparams.quantize_channels - 1)
    else:
        out = wav

    # zero pad
    # this is needed to adjust time resolution between audio and mel-spectrogram
    l, r = audio.pad_lr(out, hparams.fft_size, audio.get_hop_size())
    if l > 0 or r > 0:
        out = np.pad(out, (l, r), mode="constant", constant_values=constant_values)
    N = mel_spectrogram.shape[0]
    # if len(out) < N * audio.get_hop_size():
    #    print(N, len(out))
    assert len(out) >= N * audio.get_hop_size()

    # time resolution adjustment
    # ensure length of raw audio is multiple of hop_size so that we can use
    # transposed convolution to upsample
    out = out[:N * audio.get_hop_size()]
    assert len(out) % audio.get_hop_size() == 0

    # Write the spectrograms to disk:
    name = splitext(basename(wav_path))[0]
    audio_filename = '%s-wave.npy' % (name)
    mel_filename = '%s-feats.npy' % (name)
    np.save(os.path.join(out_dir, audio_filename),
            out.astype(out_dtype), allow_pickle=False)
    np.save(os.path.join(out_dir, mel_filename),
            mel_spectrogram.astype(np.float32), allow_pickle=False)

    # Return a tuple describing this training example:
    return (audio_filename, mel_filename, N, text)


