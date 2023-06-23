# Use the official Python base image
FROM python:3.9-slim

# Set the working directory in the container
WORKDIR /app

# Copy the lambda function code to the container
COPY src /app/src

# Install the required dependencies
RUN python3.9 -m pip install -r /src/requirements.txt -t .

# Set the entrypoint to the Lambda function handler
CMD ["src/lambda_function.lambda_handler"]
