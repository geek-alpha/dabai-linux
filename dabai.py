from amazing_agent_dingding import amazing_agent
from screen_shot import take_screenshot
from dabai_voice import play_audio
from dabai_ears import record_and_transcribe
def dabai():
        while True:
            transcription = record_and_transcribe()
            screen_shot_path = take_screenshot("screen_shot.png")
            respose=amazing_agent(transcription, "")  # Replace with actual image path
            print(respose)
            play_audio(respose, file_path="output.mp3")
            print(f"Transcription: {transcription}")
if __name__ == "__main__":
    dabai()
