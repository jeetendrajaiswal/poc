"""Elastic Beanstalk entrypoint — exposes the Flask app as `application`."""
from src.webapp import app as application

if __name__ == "__main__":
    application.run(host="0.0.0.0", port=8000)
