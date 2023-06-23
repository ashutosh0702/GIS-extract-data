# Base image
FROM python:3.9

# Set the working directory
WORKDIR /app

# Copy the source code
COPY src/ /app/src/
#COPY requirements.txt /app

# Install dependencies
RUN pip install --no-cache-dir -r /app/src/requirements.txt

# Set the entry point
CMD ["python", "/app/src/lambda_function.py"]

