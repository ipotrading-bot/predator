import os
import sys
from core.notifications import send_startup_notification

# Entry point logic
if __name__ == "__main__":
    print("Predator PAIM initialized.")
    send_startup_notification()
    # Example: run the streamlit app
    # os.system("streamlit run ui/dashboard.py")
