# Base image
FROM public.ecr.aws/lambda/python:3.9

# Set the working directory
WORKDIR /app

# Copy the source code and requirements file
COPY src/lambda_function.py .
COPY src/requirements.txt .


# Install dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Set the entry point
CMD ["lambda_function.lambda_handler"]

