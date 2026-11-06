"""隔离CPU转写子进程；只加载已提供的本地模型，不隐式下载。"""
import argparse
import json
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--audio', required=True)
    parser.add_argument('--model', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    from faster_whisper import WhisperModel
    model = WhisperModel(
        args.model,
        device='cpu',
        compute_type='int8',
        cpu_threads=2,
        num_workers=1,
        local_files_only=True,
    )
    segments, _info = model.transcribe(args.audio, beam_size=1, vad_filter=True)
    rows, characters = [], 0
    for segment in segments:
        if segment.text.strip() and segment.end > segment.start:
            rows.append({'from': segment.start, 'to': segment.end, 'content': segment.text.strip()})
            characters += len(segment.text.strip())
        if len(rows) > 10000 or characters > 180000:
            raise ValueError('ASR_RESULT_TOO_LARGE')
    if not rows:
        raise ValueError('NO_SPEECH_DETECTED')
    Path(args.output).write_text(json.dumps(rows, ensure_ascii=False), encoding='utf-8')


if __name__ == '__main__':
    main()
