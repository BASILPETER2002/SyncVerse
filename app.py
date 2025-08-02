from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
import os
from PyPDF2 import PdfReader
import pytesseract
from pdf2image import convert_from_path
from werkzeug.utils import secure_filename
from dotenv import load_dotenv
import google.generativeai as genai
import json
from collections import Counter
from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api._errors import TranscriptsDisabled, NoTranscriptFound
from urllib.parse import urlparse, parse_qs
from bs4 import BeautifulSoup
import requests
import shutil
from google.cloud import speech
import io
import base64

os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = "service_account.json"
# Load environment variables
load_dotenv()
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
genai.configure(api_key=GOOGLE_API_KEY)

model = genai.GenerativeModel("models/gemini-1.5-flash")

app = Flask(__name__)
CORS(app)

UPLOAD_FOLDER = "uploads"
TEXT_FOLDER = "extracted_texts"
ANALYTICS_FILE = "analytics.json"
POPPLER_PATH = r"C:\\poppler\\poppler-24.08.0\\Library\\bin"
pytesseract.pytesseract.tesseract_cmd = r"C:\\Program Files\\Tesseract-OCR\\tesseract.exe"

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(TEXT_FOLDER, exist_ok=True)
if not os.path.exists(ANALYTICS_FILE):
    with open(ANALYTICS_FILE, "w") as f:
        json.dump({}, f)

USERS_FILE = "users.json"
if not os.path.exists(USERS_FILE):
    with open(USERS_FILE, "w") as f:
        json.dump({}, f)


def extract_text_with_ocr(pdf_path):
    text = ""
    try:
        reader = PdfReader(pdf_path)
        for page in reader.pages:
            page_text = page.extract_text()
            if page_text:
                text += page_text
    except Exception as e:
        print(f"PDF extract_text error: {e}")
    if not text.strip():
        try:
            images = convert_from_path(pdf_path, poppler_path=POPPLER_PATH)
            for image in images:
                text += pytesseract.image_to_string(image)
        except Exception as e:
            print(f"OCR error: {e}")
    return text.strip()


def update_analytics(username, filename, word_count, page_count):
    try:
        with open(ANALYTICS_FILE, "r") as f:
            data = json.load(f)
    except Exception:
        data = {}

    if username not in data:
        data[username] = {"files": [], "queries": []}

    data[username]["files"] = [f for f in data[username]["files"] if f["filename"] != filename]
    data[username]["files"].append({"filename": filename, "words": word_count, "pages": page_count})

    with open(ANALYTICS_FILE, "w") as f:
        json.dump(data, f)


def log_query(username, query):
    try:
        with open(ANALYTICS_FILE, "r") as f:
            data = json.load(f)
    except Exception:
        data = {}

    if username not in data:
        data[username] = {"files": [], "queries": []}
    data[username]["queries"].append(query)

    with open(ANALYTICS_FILE, "w") as f:
        json.dump(data, f)

@app.route("/register", methods=["POST"])
def register():
    data = request.get_json()
    username = data.get("username", "").strip()
    password = data.get("password", "").strip()

    if not username or not password:
        return jsonify({"success": False, "error": "Username and password required."}), 400

    try:
        with open(USERS_FILE, "r") as f:
            users = json.load(f)

        if username in users:
            return jsonify({"success": False, "error": "Username already exists."}), 409

        users[username] = password  # ✅ Store plain text or use hash (optional)

        with open(USERS_FILE, "w") as f:
            json.dump(users, f)

        return jsonify({"success": True, "message": "Registration successful."})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route("/login", methods=["POST"])
def login():
    data = request.get_json()
    username = data.get("username", "").strip()
    password = data.get("password", "").strip()

    if not username or not password:
        return jsonify({"success": False, "error": "Username and password required."}), 400

    try:
        with open(USERS_FILE, "r") as f:
            users = json.load(f)

        if users.get(username) == password:
            return jsonify({"success": True, "message": "Login successful."})
        else:
            return jsonify({"success": False, "error": "Invalid username or password."}), 401

    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route("/upload", methods=["POST"])
def upload():
    try:
        file = request.files.get("file")
        username = request.form.get("username", "guest")
        if not file:
            return jsonify({"success": False, "error": "No file uploaded"}), 400

        user_folder = os.path.join(UPLOAD_FOLDER, username)
        text_folder = os.path.join(TEXT_FOLDER, username)
        os.makedirs(user_folder, exist_ok=True)
        os.makedirs(text_folder, exist_ok=True)

        filename = secure_filename(file.filename)
        filepath = os.path.join(user_folder, filename)
        file.save(filepath)

        text = extract_text_with_ocr(filepath)
        text_path = os.path.join(text_folder, filename + ".txt")
        with open(text_path, "w", encoding="utf-8") as f:
            f.write(text)

        word_count = len(text.split())
        page_count = len(PdfReader(filepath).pages)
        update_analytics(username, filename, word_count, page_count)

        return jsonify({"success": True, "filename": filename})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/files/<username>", methods=["GET"])
def list_files(username):
    folder = os.path.join(UPLOAD_FOLDER, username)
    if not os.path.exists(folder):
        return jsonify({"files": []})
    return jsonify({"files": os.listdir(folder)})


@app.route("/preview/<username>/<filename>", methods=["GET"])
def preview(username, filename):
    path = os.path.join(TEXT_FOLDER, username, filename + ".txt")
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return jsonify({"text": f.read()})
    return jsonify({"error": "Not found"}), 404


@app.route("/ask", methods=["POST"])
def ask():
    try:
        data = request.get_json()
        question = data.get("question")
        username = data.get("username", "guest")
        filename = data.get("filename")
        path = os.path.join(TEXT_FOLDER, username, filename + ".txt")
        if not os.path.exists(path):
            return jsonify({"error": "Text not found"})

        with open(path, "r", encoding="utf-8") as f:
            content = f.read()

        safe_text = content[:12000]
        convo = model.start_chat(history=[])
        response = convo.send_message(f"{question}\n\nPDF Content:\n{safe_text}")
        log_query(username, question)
        return jsonify({"answer": response.text})
    except Exception as e:
        return jsonify({"error": f"Gemini Error: {str(e)}"})


@app.route("/ask_all", methods=["POST"])
def ask_all():
    try:
        data = request.get_json()
        username = data.get("username", "guest")
        question = data.get("question", "")
        user_folder = os.path.join(TEXT_FOLDER, username)

        if not os.path.exists(user_folder):
            return jsonify({"error": "No extracted texts found."}), 404

        combined_text = ""
        for file in os.listdir(user_folder):
            with open(os.path.join(user_folder, file), "r", encoding="utf-8") as f:
                combined_text += f.read() + "\n"

        safe_text = combined_text[:15000]
        convo = model.start_chat(history=[])
        response = convo.send_message(f"{question}\n\nCombined PDF Content:\n{safe_text}")
        log_query(username, question)
        return jsonify({"answer": response.text})
    except Exception as e:
        return jsonify({"error": f"Gemini Error: {str(e)}"}), 500


@app.route("/clear/<username>", methods=["GET"])
def clear_user_data(username):
    try:
        print(f"[CLEAR] Clearing data for user: {username}")

        # Delete user uploads and text folders
        for folder_name in [UPLOAD_FOLDER, TEXT_FOLDER]:
            user_folder = os.path.join(folder_name, username)
            if os.path.exists(user_folder):
                shutil.rmtree(user_folder)
                print(f"[CLEAR] Deleted folder: {user_folder}")

        # Remove user analytics
        if os.path.exists(ANALYTICS_FILE):
            with open(ANALYTICS_FILE, "r") as f:
                data = json.load(f)
            if username in data:
                del data[username]
                with open(ANALYTICS_FILE, "w") as f:
                    json.dump(data, f)
                print(f"[CLEAR] Cleared analytics for: {username}")

        return jsonify({"success": True, "message": f"All data for '{username}' cleared."})

    except Exception as e:
        print(f"[CLEAR ERROR] {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@app.route("/uploads/<username>/<filename>", methods=["GET"])
def serve(username, filename):
    return send_from_directory(os.path.join(UPLOAD_FOLDER, username), filename)


@app.route("/search", methods=["POST"])
def search():
    data = request.get_json()
    keyword = data.get("keyword")
    username = data.get("username")
    filename = data.get("filename")
    path = os.path.join(TEXT_FOLDER, username, filename + ".txt")
    if not os.path.exists(path):
        return jsonify({"results": []})
    with open(path, "r", encoding="utf-8") as f:
        lines = f.readlines()
    matches = [line.strip() for line in lines if keyword.lower() in line.lower()]
    return jsonify({"results": matches})


@app.route("/analytics/<username>", methods=["GET"])
def analytics(username):
    try:
        with open(ANALYTICS_FILE, "r") as f:
            data = json.load(f)
        user_data = data.get(username, {"files": [], "queries": []})
        query_counts = Counter(user_data["queries"])
        top_queries = query_counts.most_common(5)
        return jsonify({
            "total_files": len(user_data["files"]),
            "total_words": sum(f["words"] for f in user_data["files"]),
            "total_pages": sum(f["pages"] for f in user_data["files"]),
            "top_queries": top_queries
        })
    except Exception as e:
        return jsonify({
            "total_files": 0,
            "total_words": 0,
            "total_pages": 0,
            "top_queries": [],
            "error": str(e)
        })


@app.route("/youtube", methods=["POST"])
def summarize_youtube():
    data = request.get_json()
    url = data.get("url", "")
    if not url:
        return jsonify({"error": "No URL provided"}), 400

    try:
        video_id = parse_qs(urlparse(url).query).get("v", [None])[0]
        if not video_id:
            return jsonify({"error": "Invalid YouTube URL"}), 400

        transcript = YouTubeTranscriptApi().fetch(video_id)
        text = " ".join([entry.text for entry in transcript])[:10000]
        convo = model.start_chat(history=[])
        response = convo.send_message(f"Summarize this YouTube transcript:\n{text}")
        return jsonify({"summary": response.text})
    except (TranscriptsDisabled, NoTranscriptFound):
        return jsonify({"error": "Transcript not available for this video"}), 404
    except Exception as e:
        return jsonify({"error": f"Gemini Error: {str(e)}"}), 500


@app.route("/webclip", methods=["POST"])
def summarize_web():
    data = request.get_json()
    url = data.get("url", "")
    if not url:
        return jsonify({"error": "No URL provided"}), 400

    try:
        response = requests.get(url, timeout=10)
        soup = BeautifulSoup(response.text, "html.parser")
        paragraphs = soup.find_all("p")
        text = " ".join(p.get_text() for p in paragraphs)
        if not text.strip():
            return jsonify({"error": "No readable text found on page"}), 400

        cleaned_text = text[:10000]
        convo = model.start_chat(history=[])
        summary = convo.send_message(f"Summarize the following webpage content:\n{cleaned_text}")

        return jsonify({"summary": summary.text})
    except Exception as e:
        return jsonify({"error": f"Gemini Error: {str(e)}"}), 500

@app.route("/voice-to-text", methods=["POST"])
def voice_to_text():
    try:
        audio_data = request.json.get("audio")
        if not audio_data:
            return jsonify({"error": "No audio data provided"}), 400

        # Decode base64 audio (from frontend)
        audio_bytes = base64.b64decode(audio_data)

        client = speech.SpeechClient()
        audio = speech.RecognitionAudio(content=audio_bytes)
        config = speech.RecognitionConfig(
            encoding=speech.RecognitionConfig.AudioEncoding.LINEAR16,
            sample_rate_hertz=16000,
            language_code="en-US",
        )

        response = client.recognize(config=config, audio=audio)

        transcript = ""
        for result in response.results:
            transcript += result.alternatives[0].transcript

        return jsonify({"text": transcript})

    except Exception as e:
        return jsonify({"error": f"Voice processing failed: {str(e)}"}), 500

    
if __name__ == "__main__":
    app.run(debug=True)
