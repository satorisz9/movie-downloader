import os
import uuid
import shutil
import subprocess
import tempfile
from flask import Flask, render_template, request, jsonify, send_file
import yt_dlp

app = Flask(__name__)

DOWNLOAD_DIR = os.path.join(os.path.dirname(__file__), "downloads")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/info", methods=["POST"])
def video_info():
    """URLから動画情報を取得する"""
    url = request.json.get("url", "").strip()
    if not url:
        return jsonify({"error": "URLを入力してください"}), 400

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "writesubtitles": True,
        "writeautomaticsub": True,
        "subtitleslangs": ["all"],
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as e:
        return jsonify({"error": f"動画情報を取得できませんでした: {e}"}), 400

    # プレイリストの場合
    if info.get("_type") == "playlist":
        entries = []
        for entry in (info.get("entries") or []):
            if entry is None:
                continue
            entries.append(_format_entry(entry))
        return jsonify({"type": "playlist", "title": info.get("title", ""), "entries": entries})

    # 単一動画の場合
    return jsonify({"type": "single", "entries": [_format_entry(info)]})


def _format_entry(info):
    """動画情報を整形する"""
    formats = []
    for f in (info.get("formats") or []):
        # 映像+音声があるフォーマットを優先表示
        has_video = f.get("vcodec", "none") != "none"
        has_audio = f.get("acodec", "none") != "none"
        ext = f.get("ext", "?")
        resolution = f.get("resolution") or f.get("format_note") or ""
        filesize = f.get("filesize") or f.get("filesize_approx")

        label_parts = []
        if resolution:
            label_parts.append(resolution)
        label_parts.append(ext)
        if has_video and has_audio:
            label_parts.append("(映像+音声)")
        elif has_video:
            label_parts.append("(映像のみ)")
        elif has_audio:
            label_parts.append("(音声のみ)")
        if filesize:
            label_parts.append(f"[{filesize / 1024 / 1024:.1f}MB]")

        formats.append({
            "format_id": f.get("format_id"),
            "label": " ".join(label_parts),
            "has_video": has_video,
            "has_audio": has_audio,
        })

    # 利用可能な字幕言語を収集
    subtitles = {}
    for lang, subs in (info.get("subtitles") or {}).items():
        subtitles[lang] = {"label": lang, "auto": False}
    for lang, subs in (info.get("automatic_captions") or {}).items():
        if lang not in subtitles:
            subtitles[lang] = {"label": lang, "auto": True}

    return {
        "id": info.get("id", ""),
        "title": info.get("title", "不明"),
        "thumbnail": info.get("thumbnail", ""),
        "duration": info.get("duration"),
        "formats": formats,
        "subtitles": subtitles,
    }


@app.route("/api/download", methods=["POST"])
def download_video():
    """動画をダウンロードしてファイルとして返す"""
    url = request.json.get("url", "").strip()
    format_id = request.json.get("format_id", "").strip()
    subtitle_lang = request.json.get("subtitle_lang", "").strip()
    embed_subs = request.json.get("embed_subs", False)
    generate_subs = request.json.get("generate_subs", False)
    whisper_lang = request.json.get("whisper_lang", "").strip()
    if not url:
        return jsonify({"error": "URLを入力してください"}), 400

    task_id = uuid.uuid4().hex
    task_dir = os.path.join(DOWNLOAD_DIR, task_id)
    os.makedirs(task_dir, exist_ok=True)

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "outtmpl": os.path.join(task_dir, "%(title).80s.%(ext)s"),
        "noplaylist": True,
    }

    if format_id:
        # 映像のみフォーマットの場合、音声もマージ
        ydl_opts["format"] = f"{format_id}+bestaudio/best/{format_id}"
        ydl_opts["merge_output_format"] = "mp4"
    else:
        ydl_opts["format"] = "best"

    # 字幕の設定
    if subtitle_lang:
        ydl_opts["writesubtitles"] = True
        ydl_opts["writeautomaticsub"] = True
        ydl_opts["subtitleslangs"] = [subtitle_lang]
        if embed_subs:
            ydl_opts["postprocessors"] = ydl_opts.get("postprocessors", []) + [
                {"key": "FFmpegEmbedSubtitle"}
            ]
            if not format_id:
                ydl_opts["merge_output_format"] = "mp4"

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
    except yt_dlp.utils.DownloadError as e:
        shutil.rmtree(task_dir, ignore_errors=True)
        return jsonify({"error": f"ダウンロードに失敗しました: {e}"}), 400

    # ダウンロードされたファイルを探す
    files = [f for f in os.listdir(task_dir) if not f.endswith((".srt", ".vtt", ".ass"))]
    if not files:
        shutil.rmtree(task_dir, ignore_errors=True)
        return jsonify({"error": "ファイルが見つかりません"}), 500

    video_file = files[0]
    video_path = os.path.join(task_dir, video_file)

    # Whisperで字幕生成
    if generate_subs:
        try:
            video_path = _generate_subtitles(video_path, task_dir, whisper_lang)
            video_file = os.path.basename(video_path)
        except Exception as e:
            return jsonify({"error": f"字幕生成に失敗しました: {e}"}), 500

    return jsonify({"task_id": task_id, "filename": video_file})


def _generate_subtitles(video_path, task_dir, lang):
    """Whisperで音声から字幕を生成し、動画に埋め込む"""
    import whisper

    model = whisper.load_model("base")

    transcribe_opts = {}
    if lang:
        transcribe_opts["language"] = lang

    result = model.transcribe(video_path, **transcribe_opts)

    # SRTファイルを生成
    srt_path = os.path.join(task_dir, "generated.srt")
    with open(srt_path, "w", encoding="utf-8") as f:
        for i, seg in enumerate(result["segments"], 1):
            start = _format_srt_time(seg["start"])
            end = _format_srt_time(seg["end"])
            text = seg["text"].strip()
            f.write(f"{i}\n{start} --> {end}\n{text}\n\n")

    # ffmpegで字幕を動画に埋め込む
    base, ext = os.path.splitext(video_path)
    output_path = base + "_sub.mp4"
    subprocess.run(
        [
            "ffmpeg", "-i", video_path, "-i", srt_path,
            "-c", "copy", "-c:s", "mov_text",
            "-metadata:s:s:0", f"language={lang or 'und'}",
            "-y", output_path,
        ],
        check=True,
        capture_output=True,
    )

    return output_path


def _format_srt_time(seconds):
    """秒数をSRT形式の時間文字列に変換する"""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int((seconds % 1) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


@app.route("/api/file/<task_id>/<filename>")
def serve_file(task_id, filename):
    """ダウンロード済みファイルを配信する"""
    # パストラバーサル防止
    safe_task_id = os.path.basename(task_id)
    safe_filename = os.path.basename(filename)
    filepath = os.path.join(DOWNLOAD_DIR, safe_task_id, safe_filename)

    if not os.path.isfile(filepath):
        return jsonify({"error": "ファイルが見つかりません"}), 404

    return send_file(filepath, as_attachment=True, download_name=safe_filename)


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
