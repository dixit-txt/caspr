# Lighter than the monolith's image: no LibreOffice, no texlive, no pandoc,
# no Node/pptxgenjs, no Playwright/Chromium — those only ever served the
# report/PPTX rendering path, which now lives in report-render-service's
# own (much heavier) image. This service still needs weasyprint's runtime
# libs because token_checker.py and publish_utils.py use it directly for
# unrelated reasons (token estimation / publish HTML->PDF), not because of
# anything report_util-related.
FROM python:3.11.8

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        libpango-1.0-0 libpangocairo-1.0-0 libgdk-pixbuf2.0-0 libffi-dev \
        shared-mime-info curl && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

RUN python3 -m pip install --upgrade pip
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

EXPOSE 8000

CMD ["python", "main.py"]
