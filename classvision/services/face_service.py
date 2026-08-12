import os
import cv2
import numpy as np
import pandas as pd
from PIL import Image
import base64
import io
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Paths - using existing structure
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROJECT_ROOT = os.path.dirname(BASE_DIR)
HAAR_CASCADE_PATH = os.path.join(PROJECT_ROOT, 'haarcascade_frontalface_default.xml')
TRAIN_IMAGE_PATH = os.path.join(PROJECT_ROOT, 'TrainingImage')
TRAIN_LABEL_PATH = os.path.join(PROJECT_ROOT, 'TrainingImageLabel', 'Trainner.yml')
STUDENT_DETAILS_PATH = os.path.join(PROJECT_ROOT, 'StudentDetails', 'studentdetails.csv')
DB_PATH = os.path.join(BASE_DIR, 'data', 'classvision.db')

# Image preprocessing constants
FACE_SIZE = (100, 100)  # Standard size for LBPH training
MIN_FACE_SIZE = 60      # Minimum face size in pixels
MIN_BRIGHTNESS = 25
MAX_BRIGHTNESS = 230
MIN_LAPLACIAN_VAR = 65.0  # Blur detection threshold

# Ensure directories exist
os.makedirs(TRAIN_IMAGE_PATH, exist_ok=True)
os.makedirs(os.path.dirname(TRAIN_LABEL_PATH), exist_ok=True)
os.makedirs(os.path.dirname(STUDENT_DETAILS_PATH), exist_ok=True)


class FaceRecognitionService:
    def __init__(self):
        self.detector = None
        self.recognizer = None
        self.student_data = {}
        self.load_models()

    def load_models(self):
        """Load Haar Cascade and LBPH recognizer"""
        try:
            if os.path.exists(HAAR_CASCADE_PATH):
                self.detector = cv2.CascadeClassifier(HAAR_CASCADE_PATH)
            else:
                # Fallback to OpenCV default cascade
                self.detector = cv2.CascadeClassifier(cv2.data.haarcascades + 'haarcascade_frontalface_default.xml')

            if self.detector is None or self.detector.empty():
                logger.error("Haar Cascade classifier failed to load")

            self.load_student_data()

            # Load trained model if exists
            if os.path.exists(TRAIN_LABEL_PATH):
                self.recognizer = cv2.face.LBPHFaceRecognizer_create()
                self.recognizer.read(TRAIN_LABEL_PATH)
                logger.info("LBPH Face Recognizer model loaded successfully")
        except Exception as e:
            logger.error(f"Error loading face models: {e}")

    def load_student_data(self):
        """Load student details from CSV and SQLite database into memory"""
        self.student_data = {}
        # 1. Load from SQLite DB
        try:
            import sqlite3
            if os.path.exists(DB_PATH):
                conn = sqlite3.connect(DB_PATH)
                cursor = conn.cursor()
                cursor.execute("SELECT id, name, roll_number, branch, section FROM student")
                rows = cursor.fetchall()
                for row in rows:
                    s_id = str(row[0])
                    self.student_data[s_id] = {
                        'id': row[0],
                        'name': row[1].strip(),
                        'roll_number': row[2].strip(),
                        'branch': row[3],
                        'section': row[4]
                    }
                conn.close()
        except Exception as e:
            logger.error(f"Error loading student data from DB: {e}")

        # 2. Fallback / Sync from CSV
        try:
            if os.path.exists(STUDENT_DETAILS_PATH):
                df = pd.read_csv(STUDENT_DETAILS_PATH)
                for _, row in df.iterrows():
                    enrollment = str(row['Enrollment']).strip()
                    name = str(row['Name']).strip()
                    if 'StringArray' in name:
                        name = name.split("'")[1] if "'" in name else name
                    name = name.strip()
                    if enrollment not in self.student_data:
                        self.student_data[enrollment] = {
                            'id': enrollment,
                            'name': name,
                            'roll_number': enrollment,
                            'branch': 'N/A',
                            'section': 'N/A'
                        }
        except Exception as e:
            logger.error(f"Error loading student data from CSV: {e}")

    def _decode_and_preprocess_image(self, image_data):
        """Decode base64 image into grayscale numpy array"""
        try:
            if ',' in image_data:
                image_data = image_data.split(',')[1]

            image_bytes = base64.b64decode(image_data)
            image = Image.open(io.BytesIO(image_bytes))
            image_np = np.array(image)

            # Convert to grayscale
            if len(image_np.shape) == 3 and image_np.shape[2] == 3:
                gray = cv2.cvtColor(image_np, cv2.COLOR_RGB2GRAY)
            elif len(image_np.shape) == 3 and image_np.shape[2] == 4:
                gray = cv2.cvtColor(image_np, cv2.COLOR_RGBA2GRAY)
            else:
                gray = image_np

            return gray
        except Exception as e:
            logger.error(f"Error decoding base64 image: {e}")
            return None

    def _validate_face_quality(self, face_roi):
        """Validate face ROI quality: blur, size, lighting, aspect ratio"""
        try:
            h, w = face_roi.shape[:2]

            # Check size
            if h < MIN_FACE_SIZE or w < MIN_FACE_SIZE:
                logger.warning(f"Face quality rejected: Too small ({w}x{h})")
                return False, "Face is too far from camera"

            # Check aspect ratio
            aspect_ratio = float(w) / float(h)
            if aspect_ratio < 0.65 or aspect_ratio > 1.45:
                logger.warning(f"Face quality rejected: Invalid aspect ratio ({aspect_ratio:.2f})")
                return False, "Improper face orientation"

            # Check brightness
            mean_brightness = np.mean(face_roi)
            if mean_brightness < MIN_BRIGHTNESS:
                logger.warning(f"Face quality rejected: Too dark ({mean_brightness:.1f})")
                return False, "Lighting too dark"
            if mean_brightness > MAX_BRIGHTNESS:
                logger.warning(f"Face quality rejected: Overexposed ({mean_brightness:.1f})")
                return False, "Lighting too bright"

            # Check blur via Laplacian variance
            laplacian_var = cv2.Laplacian(face_roi, cv2.CV_64F).var()
            if laplacian_var < MIN_LAPLACIAN_VAR:
                logger.warning(f"Face quality rejected: Image blurry (variance {laplacian_var:.1f})")
                return False, "Image too blurry. Hold steady"

            return True, "Quality OK"
        except Exception as e:
            logger.error(f"Error validating face quality: {e}")
            return False, "Quality check error"

    def _preprocess_face(self, face_roi):
        """Preprocess face for LBPH training and recognition"""
        try:
            resized = cv2.resize(face_roi, FACE_SIZE, interpolation=cv2.INTER_AREA)
            normalized = cv2.equalizeHist(resized)
            return normalized
        except Exception as e:
            logger.error(f"Error preprocessing face: {e}")
            return face_roi

    def check_duplicate_face(self, image_data, exclude_student_id=None):
        """
        Check if a face is already registered to another student.
        Uses LBPH prediction if trained model exists, plus image similarity checking.
        """
        try:
            gray = self._decode_and_preprocess_image(image_data)
            if gray is None:
                return None

            faces = self.detector.detectMultiScale(
                gray, scaleFactor=1.2, minNeighbors=5, minSize=(MIN_FACE_SIZE, MIN_FACE_SIZE)
            )

            if len(faces) == 0 or len(faces) > 1:
                return None

            (x, y, w, h) = faces[0]
            face_roi = gray[y:y+h, x:x+w]
            is_valid, _ = self._validate_face_quality(face_roi)
            if not is_valid:
                return None

            processed_face = self._preprocess_face(face_roi)

            # Option A: Check using trained LBPH model if present
            if self.recognizer is not None:
                label, confidence = self.recognizer.predict(processed_face)
                # LBPH confidence < 52 indicates a high-confidence match (duplicate)
                if confidence < 52.0:
                    matched_id = str(label)
                    if exclude_student_id and matched_id == str(exclude_student_id):
                        return None  # Matches self, allowed

                    s_info = self.student_data.get(matched_id)
                    s_name = s_info['name'] if isinstance(s_info, dict) else (s_info or f"Student #{matched_id}")
                    s_roll = s_info.get('roll_number', matched_id) if isinstance(s_info, dict) else matched_id

                    logger.warning(f"Duplicate face detected via LBPH model: matches {s_name} (ID: {matched_id}) with conf {confidence:.1f}")
                    return {
                        'student_id': matched_id,
                        'name': s_name,
                        'roll_number': s_roll,
                        'confidence': confidence
                    }

            # Option B: Secondary dataset comparison (comparing against saved images in TrainingImage)
            for student_dir in os.listdir(TRAIN_IMAGE_PATH):
                student_dir_path = os.path.join(TRAIN_IMAGE_PATH, student_dir)
                if not os.path.isdir(student_dir_path):
                    continue

                # Parse student ID
                try:
                    if student_dir.startswith("student_"):
                        s_id = student_dir.split("_")[1]
                    else:
                        s_id = student_dir.split("_")[0]
                except Exception:
                    continue

                if exclude_student_id and str(s_id) == str(exclude_student_id):
                    continue

                # Sample up to 5 images from directory for fast template match check
                img_files = [f for f in os.listdir(student_dir_path) if f.endswith('.jpg')][:5]
                for img_file in img_files:
                    sample_path = os.path.join(student_dir_path, img_file)
                    try:
                        sample_img = cv2.imread(sample_path, cv2.IMREAD_GRAYSCALE)
                        if sample_img is None:
                            continue
                        if sample_img.shape != FACE_SIZE:
                            sample_img = cv2.resize(sample_img, FACE_SIZE)

                        # Normalized cross-correlation
                        res = cv2.matchTemplate(processed_face, sample_img, cv2.TM_CCOEFF_NORMED)
                        max_val = cv2.minMaxLoc(res)[1]

                        if max_val > 0.82:  # High visual similarity
                            s_info = self.student_data.get(str(s_id))
                            s_name = s_info['name'] if isinstance(s_info, dict) else f"Student #{s_id}"
                            s_roll = s_info.get('roll_number', str(s_id)) if isinstance(s_info, dict) else str(s_id)

                            logger.warning(f"Duplicate face detected via template correlation: matches {s_name} (ID: {s_id}) score {max_val:.2f}")
                            return {
                                'student_id': str(s_id),
                                'name': s_name,
                                'roll_number': s_roll,
                                'confidence': float(max_val)
                            }
                    except Exception:
                        continue

            return None
        except Exception as e:
            logger.error(f"Error checking duplicate face: {e}")
            return None

    def save_captured_image(self, student_id, image_data, count):
        """Save captured face image with strict quality validation"""
        try:
            gray = self._decode_and_preprocess_image(image_data)
            if gray is None:
                return False, "Failed to decode image data"

            faces = self.detector.detectMultiScale(
                gray, scaleFactor=1.2, minNeighbors=5, minSize=(MIN_FACE_SIZE, MIN_FACE_SIZE)
            )

            if len(faces) == 0:
                return False, "No face detected in camera frame"

            if len(faces) > 1:
                return False, "Multiple faces detected. Ensure only one person is in frame"

            (x, y, w, h) = faces[0]
            face_roi = gray[y:y+h, x:x+w]

            is_valid, reason = self._validate_face_quality(face_roi)
            if not is_valid:
                return False, reason

            processed_face = self._preprocess_face(face_roi)

            student_dir = os.path.join(TRAIN_IMAGE_PATH, f"student_{student_id}")
            os.makedirs(student_dir, exist_ok=True)

            image_path = os.path.join(student_dir, f"image_{count}.jpg")
            cv2.imwrite(image_path, processed_face)

            logger.info(f"Successfully saved high-quality face image {count} for student {student_id}")
            return True, "Image captured successfully"
        except Exception as e:
            logger.error(f"Error saving captured face image: {e}")
            return False, f"Server error: {str(e)}"

    def train_model(self):
        """
        Train LBPH face recognizer with captured images.
        CRITICAL FIX: Do NOT run detectMultiScale on already-cropped 100x100 face images!
        """
        try:
            recognizer = cv2.face.LBPHFaceRecognizer_create(radius=1, neighbors=8, grid_x=8, grid_y=8)
            faces = []
            ids = []

            if not os.path.exists(TRAIN_IMAGE_PATH):
                raise Exception("TrainingImage directory does not exist")

            student_dirs = os.listdir(TRAIN_IMAGE_PATH)
            if not student_dirs:
                raise Exception("No student image directories found in TrainingImage/")

            for student_dir in student_dirs:
                student_path = os.path.join(TRAIN_IMAGE_PATH, student_dir)
                if not os.path.isdir(student_path):
                    continue

                # Extract student ID from folder name (supports student_{id} or {id}_{name})
                student_id = None
                if student_dir.startswith("student_"):
                    try:
                        student_id = int(student_dir.split("_")[1])
                    except ValueError:
                        continue
                else:
                    try:
                        student_id = int(student_dir.split("_")[0])
                    except ValueError:
                        continue

                if student_id is None:
                    continue

                image_files = [f for f in os.listdir(student_path) if f.lower().endswith(('.jpg', '.jpeg', '.png'))]
                valid_count = 0

                for image_file in image_files:
                    image_path = os.path.join(student_path, image_file)
                    try:
                        # Direct PIL read converted to grayscale
                        face_img = Image.open(image_path).convert('L')
                        face_np = np.array(face_img, 'uint8')

                        # Resize and normalize image directly (do NOT run detectMultiScale on cropped face!)
                        if face_np.shape != FACE_SIZE:
                            face_np = cv2.resize(face_np, FACE_SIZE, interpolation=cv2.INTER_AREA)

                        face_np = cv2.equalizeHist(face_np)

                        faces.append(face_np)
                        ids.append(student_id)
                        valid_count += 1
                    except Exception as img_err:
                        logger.warning(f"Skipping unreadable image {image_file}: {img_err}")
                        continue

                logger.info(f"Loaded {valid_count} training samples for student ID {student_id}")

            if len(faces) == 0:
                raise Exception("No valid training images were found to train the model")

            logger.info(f"Training LBPH model with {len(faces)} total face images across {len(set(ids))} students...")
            recognizer.train(faces, np.array(ids))

            os.makedirs(os.path.dirname(TRAIN_LABEL_PATH), exist_ok=True)
            recognizer.save(TRAIN_LABEL_PATH)

            self.recognizer = recognizer
            self.load_student_data()  # Reload student lookup dictionary
            logger.info("LBPH Model trained and saved successfully to Trainner.yml!")
            return True
        except Exception as e:
            logger.error(f"Error training face recognition model: {e}")
            raise e

    def recognize_face(self, image_data):
        """
        Recognize face from camera image data.
        Returns matching student dict if recognized, or error description.
        """
        try:
            if not self.recognizer:
                # Reload model if exists
                if os.path.exists(TRAIN_LABEL_PATH):
                    self.recognizer = cv2.face.LBPHFaceRecognizer_create()
                    self.recognizer.read(TRAIN_LABEL_PATH)
                else:
                    return None, "Face recognition model is not trained yet"

            gray = self._decode_and_preprocess_image(image_data)
            if gray is None:
                return None, "Failed to decode camera frame"

            faces = self.detector.detectMultiScale(
                gray, scaleFactor=1.2, minNeighbors=5, minSize=(MIN_FACE_SIZE, MIN_FACE_SIZE)
            )

            if len(faces) == 0:
                return None, "No face detected in camera frame"

            if len(faces) > 1:
                return None, "Multiple faces detected in camera frame"

            (x, y, w, h) = faces[0]
            face_roi = gray[y:y+h, x:x+w]

            is_valid, reason = self._validate_face_quality(face_roi)
            if not is_valid:
                return None, reason

            processed_face = self._preprocess_face(face_roi)
            label, confidence = self.recognizer.predict(processed_face)

            # LBPH returns distance measure: LOWER value = HIGHER similarity!
            # Confidence threshold: distance < 68 is acceptable match
            if confidence < 68.0:
                student_id = str(label)
                # Refresh student data if missing key
                if student_id not in self.student_data:
                    self.load_student_data()

                s_info = self.student_data.get(student_id)

                if isinstance(s_info, dict):
                    student_name = s_info['name']
                    roll_number = s_info['roll_number']
                    branch = s_info['branch']
                    section = s_info['section']
                else:
                    student_name = str(s_info) if s_info else f"Student #{student_id}"
                    roll_number = student_id
                    branch = "N/A"
                    section = "N/A"

                logger.info(f"SUCCESS: Recognized {student_name} (ID: {student_id}, Roll: {roll_number}) with distance {confidence:.1f}")
                return {
                    'student_id': int(student_id),
                    'name': student_name,
                    'roll_number': roll_number,
                    'branch': branch,
                    'section': section,
                    'confidence': round(confidence, 1)
                }, None
            else:
                logger.info(f"REJECTED: Face distance {confidence:.1f} exceeds threshold 68.0 -> Unknown Face")
                return None, "Unknown face detected"

        except Exception as e:
            logger.error(f"Error during face recognition: {e}")
            return None, f"Recognition error: {str(e)}"


# Global instance
face_service = FaceRecognitionService()
