pipeline {
  agent any

  stages {
    stage('Checkout') {
      steps {
        checkout scm
      }
    }
  
    stage('Python check') {
      steps {
        sh 'docker run --rm -v "$PWD:/app" -w /app python:3.12-slim python -m py_compile app.py''
      }
    }

    stage('Docker build'){
      steps {
	sh 'docker build -t wishlist:test .'
      }
    }

    stage('smoke test'){
      steps {
        sh './smoke-test.sh'
      }
    }
  }
}
