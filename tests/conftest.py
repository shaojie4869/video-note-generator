import os
import tempfile

os.environ["STUDY_BUDDY_DATA"] = tempfile.mkdtemp(prefix="study-video-tests-")
