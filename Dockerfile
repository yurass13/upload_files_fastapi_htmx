FROM python:3.12.4-alpine3.20

WORKDIR /usr/src/app

ENV PYTHONDONTWRITEBYTECODE 1
ENV PYTHONUNBUFFERED 1

COPY poetry.lock pyproject.toml ./

RUN pip install --upgrade pip && pip install poetry && poetry export --output=requirements.txt
RUN pip install -r requirements.txt

COPY static ./static
COPY templates ./templates
COPY web ./web
