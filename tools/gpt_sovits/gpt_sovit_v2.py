import wave
import contextlib
import re
import json
import requests
import os
import uuid
# 获取调用这个模块所在文件的目录
current_root_dir = os.path.dirname(os.path.abspath(__file__))
config_path = os.path.join(current_root_dir, "gpt_sovits.json")

# 使用 POST 方法调用 TTS 接口的函数
def post_tts(data):
    url = "http://127.0.0.1:9880/tts"
    headers = {
        'Connection': 'close'
    }
    response = requests.post(url, json=data, headers=headers)
    if response.status_code == 200:
        return response.content  # 返回音频流
    else:
        return response.json()  # 返回错误信息

# 控制服务器的函数
def control_server(command):
    url = "http://127.0.0.1:9880/control"
    params = {"command": command}
    response = requests.get(url, params=params)
    return response.status_code

# 设置 GPT 权重的函数
def set_gpt_weights(weights_path):
    url = "http://127.0.0.1:9880/set_gpt_weights"
    params = {"weights_path": weights_path}
    response = requests.get(url, params=params)
    if response.status_code == 200:
        return "success"
    else:
        return response.json()  # 返回错误信息

# 设置 Sovits 权重的函数
def set_sovits_weights(weights_path):
    url = "http://127.0.0.1:9880/set_sovits_weights"
    params = {"weights_path": weights_path}
    response = requests.get(url, params=params)
    if response.status_code == 200:
        return "success"
    else:
        return response.json()  # 返回错误信息
    



class gpt_sovits:
    def calculate_audio_duration(self,audio_path):
        with contextlib.closing(wave.open(audio_path, 'r')) as f:
            frames = f.getnframes()
            rate = f.getframerate()
            return frames / float(rate)

    def time(self, text,character_name,is_enable=True):
        with open(config_path, "r", encoding="utf-8") as f:
            gpt_config = json.load(f)
            character=gpt_config[character_name]
        #ref_audio_path,prompt_text,GPT_weights_path,Sovits_weights_path
        text_lang="zh"
        prompt_lang="zh"
        text_split_method="cut5"
        batch_size=1
        media_type="wav"
        GPT_weights_path=character["GPT_weights_path"]
        Sovits_weights_path=character["Sovits_weights_path"]
        if is_enable == False:
            return (None,)
        if GPT_weights_path != "":
            set_gpt_weights(GPT_weights_path)
        if Sovits_weights_path != "":
            set_sovits_weights(Sovits_weights_path)
        # 如果text_lang=zh,删除text中所有的非中文字符（包含英文标点，不包含中文标点）
        if text_lang == "zh":
            text = re.sub(r'[^\u4e00-\u9fa5，。！？；：、（）《》“”‘’]', '', text)
        data = {
    "text": text,
    "text_lang": text_lang,
    "ref_audio_path": character["ref_audio_path"],
    "prompt_text": character["prompt_text"],
    "prompt_lang": prompt_lang,
    "text_split_method": text_split_method,
    "batch_size": batch_size,
    "media_type": media_type,
    "streaming_mode": False,
}
        audio_stream = post_tts(data)
        # 如果audio_stream是一个字典
        if isinstance(audio_stream, dict):
            print("audio_stream is a dict:", audio_stream)
        # 判断当前目录是否存在audio文件夹，如果不存在则创建
        audio_dir = os.path.join(current_root_dir, "audio")
        if not os.path.exists(audio_dir):
            os.makedirs(audio_dir)
        #timestamp = int(time.time() * 1000)
        full_audio_path = os.path.join(audio_dir, f"{uuid.uuid4()}.{media_type}")
        with open(full_audio_path, "wb") as f:
            f.write(audio_stream)
        out = full_audio_path
        audio_path = out
        #waveform, sample_rate = torchaudio.load(audio_path)
        #audio_out = {"waveform": waveform.unsqueeze(0), "sample_rate": sample_rate}
        return audio_path
def save():
    with open(config_path, "r", encoding='utf-8') as f:
        gpt_config = json.load(f)
    gpt_config["莱卡恩"]={
        "ref_audio_path":"D:/AI/GPT-SoVITS-v3lora-20250228/refer_audio/莱卡恩/不过如果真如阁下所说，这位发帖人只是在恶作剧的话，倒也无妨。.wav",
        "prompt_text":"不过如果真如阁下所说，这位发帖人只是在恶作剧的话，倒也无妨。",
        "GPT_weights_path":"D:\\AI\\GPT-SoVITS-v3lora-20250228\\GPT_weights_v3\\莱卡恩-e10.ckpt",
        "Sovits_weights_path":"D:\\AI\\GPT-SoVITS-v3lora-20250228\\SoVITS_weights_v3\\莱卡恩_e10_s560.pth",
        "character_style": "这个音色是男声，适合冷静、理智、沉稳的角色，声音清晰、低沉，带有一定的磁性和深度，适合表现智慧、权威、神秘等特质。"

        }
    with open(config_path, "w", encoding='utf-8') as f:
        json.dump(gpt_config, f, indent=4, ensure_ascii=False)

def save1():
    with open(config_path, "r", encoding='utf-8') as f:
        gpt_config = json.load(f)
    gpt_config["星见雅"]={
        "ref_audio_path":"D:/AI/GPT-SoVITS-v3lora-20250228/refer_audio/雅/难过_sad/【难过_sad】就像你追求真正的正义一样，蜜瓜就是蜜瓜绝不会变成假的。.wav",
        "prompt_text":"就像你追求真正的正义一样，蜜瓜就是蜜瓜绝不会变成假的。",
        "GPT_weights_path":"D:\\AI\\GPT-SoVITS-v3lora-20250228\\GPT_weights_v3\\雅-e10.ckpt",
        "Sovits_weights_path":"D:\\AI\\GPT-SoVITS-v3lora-20250228\\SoVITS_weights_v3\\雅_e10_s170.pth",
            "character_style": "这个音色是女声，有点高冷、清冷的感觉，适合表现独立、坚强、聪明的女性角色。"

        }
    with open(config_path, "w", encoding='utf-8') as f:
        json.dump(gpt_config, f, indent=4, ensure_ascii=False)

def save2():
    with open(config_path, "r", encoding='utf-8') as f:
        gpt_config = json.load(f)
    gpt_config["苍角"]={
        "ref_audio_path":"D:/AI/GPT-SoVITS-v3lora-20250228/refer_audio/苍角/另外关于吃的我完全没有忌口，只要是好吃的，我什么都可以吃啊。.wav",
        "prompt_text":"另外关于吃的我完全没有忌口，只要是好吃的，我什么都可以吃啊。",
        "GPT_weights_path":"D:\\AI\\GPT-SoVITS-v3lora-20250228\\GPT_weights_v3\\苍角-e10.ckpt",
        "Sovits_weights_path":"D:\\AI\\GPT-SoVITS-v3lora-20250228\\SoVITS_weights_v3\\苍角_e10_s110.pth",
        "character_style": "这个音色是童声，适合表现活泼、可爱、天真无邪的角色。声音清脆、明亮，带有一定的稚气和童趣，适合表现童真、纯真、快乐等特质。"

        }
    with open(config_path, "w", encoding='utf-8') as f:
        json.dump(gpt_config, f, indent=4, ensure_ascii=False)

def save3():
    with open(config_path, "r", encoding='utf-8') as f:
        gpt_config = json.load(f)
    gpt_config["光头强"]={
        "ref_audio_path":"D:/AI/GPT-SoVITS-v3lora-20250228/refer_audio/光头强/没人哪，难道我听错了？算了，吃饱了睡会儿去，晚上还得干活.wav",
        "prompt_text":"没人哪，难道我听错了？算了，吃饱了睡会儿去，晚上还得干活。",
        "GPT_weights_path":"D:\\AI\\GPT-SoVITS-v3lora-20250228\\GPT_weights_v3\\guangtouqiang-e15.ckpt",
        "Sovits_weights_path":"D:\\AI\\GPT-SoVITS-v3lora-20250228\\SoVITS_weights_v3\\guangtouqiang_e4_s100.pth",
        "character_style": "这个音色是男声，适合表现憨厚、幽默、乐观的角色。声音浑厚、低沉，带有一定的亲和力和幽默感，适合表现乐观、开朗、幽默等特质。"

        }
    with open(config_path, "w", encoding='utf-8') as f:
        json.dump(gpt_config, f, indent=4, ensure_ascii=False)
def save4():
    with open(config_path, "r", encoding='utf-8') as f:
        gpt_config = json.load(f)
    gpt_config["可莉"] = {
        "ref_audio_path": "D:/AI/GPT-SoVITS-v3lora-20250228/refer_audio/可莉/【默认】玩得太开心，忘、忘在脑后了….wav",
        "prompt_text": "玩得太开心，忘、忘在脑后了…",
        "GPT_weights_path": "D:\\AI\\GPT-SoVITS-v3lora-20250228\\GPT_weights_v3\\可莉_ZH-e10.ckpt",
        "Sovits_weights_path": "D:\\AI\\GPT-SoVITS-v3lora-20250228\\SoVITS_weights_v3\\可莉_ZH_e10_s530_l32.pth"
    }
    gpt_config["奶龙"] = {
        "ref_audio_path": "D:/AI/GPT-SoVITS-v3lora-20250228/refer_audio/奶龙/啊！这是什么枪？帅是帅，但你肯定没用过工兵铲.wav",
        "prompt_text": "啊！这是什么枪？帅是帅，但你肯定没用过工兵铲。",
        "GPT_weights_path": "D:\\AI\\GPT-SoVITS-v3lora-20250228\\GPT_weights_v3\\奶龙-e.ckpt",
        "Sovits_weights_path": "D:\\AI\\GPT-SoVITS-v3lora-20250228\\SoVITS_weights_v3\\奶龙_e8_s20099.pth"
    }
    with open(config_path, "w", encoding='utf-8') as f:
        json.dump(gpt_config, f, indent=4, ensure_ascii=False)

def save_all():
    gpt_config = {}
    base_path = r"D:\AI\GPT-SoVITS-v3lora-20250228\refer_audio"
    #查找目录下的文件夹
    folders = [f for f in os.listdir(base_path) if os.path.isdir(os.path.join(base_path, f))]
    print(folders)
    character_style = ""
    prompt_text = ""
    GPT_weights_path = ""
    Sovits_weights_path = ""
    face = ""
    for folder in folders:
        files = os.listdir(os.path.join(base_path, folder))
        for file in files:
            if file.endswith(".wav"):
                ref_audio_path = os.path.join(base_path, folder, file)
                prompt_text = os.path.splitext(file)[0]
            elif file.endswith(".txt"):
                character_style = os.path.splitext(file)[0]
            elif file.endswith(".ckpt"):
                GPT_weights_path = os.path.join(base_path, folder, file)
            elif file.endswith(".pth"):
                Sovits_weights_path = os.path.join(base_path, folder, file)
            elif file.endswith(".md"):
                face = os.path.splitext(file)[0]
        gpt_config[folder] = {
            "ref_audio_path": ref_audio_path,
            "prompt_text": prompt_text,
            "GPT_weights_path": GPT_weights_path,
            "Sovits_weights_path": Sovits_weights_path,
            "character_style": character_style,
            "face": face
        }
        with open(config_path, "w", encoding='utf-8') as f:
            json.dump(gpt_config, f, indent=4, ensure_ascii=False)

    return config_path
if __name__ == "__main__":
    save_all()
    #gpt_sovits().time("我是光头强啊，你怎么称呼？","光头强")
    #print(gpt_sovits().time("我是光头强啊，你怎么称呼？","星见雅"))