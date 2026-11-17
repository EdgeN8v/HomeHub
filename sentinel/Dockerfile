FROM python:3.12-slim
ENV TZ=Asia/Shanghai PYTHONUNBUFFERED=1
COPY sentinel.py recovery.py agent.py machines.py /srv/
CMD ["python", "/srv/sentinel.py"]
