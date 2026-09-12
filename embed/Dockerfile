FROM python:3.12-slim

WORKDIR /app

ARG BGE_REVISION=5c38ec7c405ec4b44b94cc5a9bb96e735b38267a
ARG RERANK_REVISION=233902d25c440f23af6f7d6e94d2946bac0bee0a
ENV BGE_REVISION=${BGE_REVISION} \
    RERANK_REVISION=${RERANK_REVISION}

RUN pip install --no-cache-dir \
    fastapi==0.116.1 \
    huggingface-hub==0.34.4 \
    numpy==2.2.6 \
    onnx==1.18.0 \
    onnxruntime==1.22.1 \
    transformers==4.57.1 \
    uvicorn==0.35.0

COPY prepare_models.py /tmp/prepare_models.py
RUN python /tmp/prepare_models.py && rm /tmp/prepare_models.py
COPY app.py /app/app.py

EXPOSE 8080

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]
