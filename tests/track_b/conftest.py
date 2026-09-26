"""Track B test setup: show real exception text in AppTest failures (the app itself hides it)."""
import os

os.environ.setdefault("STREAMLIT_CLIENT_SHOW_ERROR_DETAILS", "full")
