import pyttsx3
import threading


class VoiceNotificationService:

    def speak(self, text):
        if not text:
            return

        def _say():
            try:
                engine = pyttsx3.init()
                engine.setProperty("rate", 155)
                engine.setProperty("volume", 1.0)
                engine.say(text)
                engine.runAndWait()
                engine.stop()
            except Exception as e:
                print("Voice Error:", e)

        threading.Thread(target=_say, daemon=True).start()

    def announce_attendance(self, student_name, roll_number):
        self.speak(f"Welcome {student_name}. Attendance marked successfully.")
    def announce_attendance_marked(self, student_name):
        self.speak(f"Welcome {student_name}. Attendance marked successfully.")

    def announce_welcome(self, student_name):
        self.speak(f"Welcome {student_name}")

    def announce_error(self, error_message):
        self.speak(f"Attention. {error_message}")

    def announce_face_captured(self):
        self.speak("Face detected. Capturing dataset images.")

    def announce_training_success(self):
        self.speak("Face recognition model trained successfully.")

    def announce_duplicate_face(self, existing_name, existing_roll):
        self.speak(
            f"This face is already registered to {existing_name}, roll number {existing_roll}."
        )

    def announce_camera_error(self):
        self.speak("Unable to access the camera.")

    def announce_no_face(self):
        self.speak("No face detected.")

    def announce_unknown_face(self):
        self.speak("Unknown face detected.")

    def announce_already_marked(self):
        self.speak("Attendance has already been recorded.")

    def announce_session_complete(self):
        self.speak("Attendance session completed successfully.")


voice_service = VoiceNotificationService()