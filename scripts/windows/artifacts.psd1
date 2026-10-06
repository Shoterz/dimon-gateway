# Pinned downloads for Windows local mode. Every file is fetched from a fixed
# release or revision and verified by SHA-256 before use, so every machine
# installs exactly the artifacts that were tested together.
@{
    Runtimes = @{
        LlamaVulkan = @{
            Url    = 'https://github.com/ggml-org/llama.cpp/releases/download/b11255/llama-b11255-bin-win-vulkan-x64.zip'
            Sha256 = '3dae91806111ac4a464fb5a6db45188ba870a7e89bfa0a203750115fcef20a31'
            Bytes  = 33078779
            Dir    = 'runtimes\llama-cpp'
        }
        LlamaCpu = @{
            Url    = 'https://github.com/ggml-org/llama.cpp/releases/download/b11255/llama-b11255-bin-win-cpu-x64.zip'
            Sha256 = '13bef0d15c315b153603a610a6652e2460ae9202bb1b22832eed225ccbc7fcc7'
            Bytes  = 19165044
            Dir    = 'runtimes\llama-cpp'
        }
        WhisperCpu = @{
            Url    = 'https://github.com/ggml-org/whisper.cpp/releases/download/b5130/whisper-bin-x64.zip'
            Sha256 = 'f9ec6c52a2e949b62ab51fa21d0d497958f9e41c3010c157c4e42932d5316f3c'
            Bytes  = 8573270
            Dir    = 'runtimes\whisper-cpp'
        }
    }
    # Face registration and confirmation (OpenCV Zoo, pinned revision). Needed
    # in every mode, because every transaction is confirmed by face.
    FaceModels = @{
        FaceDetector = @{
            Url    = 'https://media.githubusercontent.com/media/opencv/opencv_zoo/47534e27c9851bb1128ccc0102f1145e27f23f98/models/face_detection_yunet/face_detection_yunet_2023mar.onnx'
            Sha256 = '8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4'
            Bytes  = 232589
            File   = 'models\face\face_detection_yunet_2023mar.onnx'
        }
        FaceRecognizer = @{
            Url    = 'https://media.githubusercontent.com/media/opencv/opencv_zoo/47534e27c9851bb1128ccc0102f1145e27f23f98/models/face_recognition_sface/face_recognition_sface_2021dec.onnx'
            Sha256 = '0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79'
            Bytes  = 38696353
            File   = 'models\face\face_recognition_sface_2021dec.onnx'
        }
    }
    # Voiceprint for hands-free confirmation of payments under S$200
    # (WeSpeaker ResNet34, VoxCeleb, CC-BY-4.0, pinned revision).
    VoiceModels = @{
        SpeakerEmbedding = @{
            Url    = 'https://huggingface.co/Wespeaker/wespeaker-voxceleb-resnet34-LM/resolve/f0c48c298fd835726c27956a5d617bad7115627e/voxceleb_resnet34_LM.onnx'
            Sha256 = '7bb2f06e9df17cdf1ef14ee8a15ab08ed28e8d0ef5054ee135741560df2ec068'
            Bytes  = 26530309
            File   = 'models\voice\voxceleb_resnet34_LM.onnx'
        }
    }
    Models = @{
        Qwen = @{
            Url    = 'https://huggingface.co/mradermacher/Huihui-Qwen3.5-9B-abliterated-GGUF/resolve/9f646d7eda193ddf2348134f3bff3d49eed7a2c6/Huihui-Qwen3.5-9B-abliterated.Q5_K_M.gguf'
            Sha256 = '946072b16f5d672e60357410f900888f6522b3ae49f241b5bf2142cb89637fb6'
            Bytes  = 6522004864
            File   = 'models\Huihui-Qwen3.5-9B-abliterated.Q5_K_M.gguf'
        }
        Whisper = @{
            Url    = 'https://huggingface.co/ggerganov/whisper.cpp/resolve/5359861c739e955e79d9a303bcbc70fb988958b1/ggml-small.en-q5_1.bin'
            Sha256 = 'bfdff4894dcb76bbf647d56263ea2a96645423f1669176f4844a1bf8e478ad30'
            Bytes  = 190098681
            File   = 'models\ggml-small.en-q5_1.bin'
        }
        KokoroModel = @{
            Url    = 'https://huggingface.co/hexgrad/Kokoro-82M/resolve/f3ff3571791e39611d31c381e3a41a3af07b4987/kokoro-v1_0.pth'
            Sha256 = '496dba118d1a58f5f3db2efc88dbdc216e0483fc89fe6e47ee1f2c53f18ad1e4'
            Bytes  = 327212226
            File   = 'models\kokoro\kokoro-v1_0.pth'
        }
        KokoroConfig = @{
            Url    = 'https://huggingface.co/hexgrad/Kokoro-82M/resolve/f3ff3571791e39611d31c381e3a41a3af07b4987/config.json'
            Sha256 = '5abb01e2403b072bf03d04fde160443e209d7a0dad49a423be15196b9b43c17f'
            Bytes  = 2351
            File   = 'models\kokoro\config.json'
        }
        KokoroVoice = @{
            Url    = 'https://huggingface.co/hexgrad/Kokoro-82M/resolve/f3ff3571791e39611d31c381e3a41a3af07b4987/voices/af_heart.pt'
            Sha256 = '0ab5709b8ffab19bfd849cd11d98f75b60af7733253ad0d67b12382a102cb4ff'
            Bytes  = 523425
            File   = 'models\kokoro\af_heart.pt'
        }
    }
    KokoroPackages = @(
        'torch==2.14.0',
        'kokoro==0.9.4',
        'misaki[en]==0.9.4',
        'spacy==3.8.16',
        'fastapi>=0.136,<0.137',
        'uvicorn>=0.46,<0.55',
        'numpy>=2,<3',
        'https://github.com/explosion/spacy-models/releases/download/en_core_web_sm-3.8.0/en_core_web_sm-3.8.0-py3-none-any.whl'
    )
}
