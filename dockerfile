FROM python:3.10

COPY src /src

COPY requirements.txt src/requirements.txt

RUN pip install -r src/requirements.txt
