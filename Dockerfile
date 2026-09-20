FROM public.ecr.aws/lambda/python:3.12

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Model, vocab and feature order are baked into the image so a running Lambda
# can never drift onto a different model than its decisions are stamped with.
COPY artifacts/model.json artifacts/vocab.json artifacts/feature_order.json ${LAMBDA_TASK_ROOT}/artifacts/
COPY src/features.py src/handler.py ${LAMBDA_TASK_ROOT}/

CMD ["handler.lambda_handler"]
