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
        sh 'docker run --rm -v jenkins_jenkins_home:/jenkins_home -w /jenkins_home/workspace/wishlist-scheduler python:3.12-slim python -m py_compile app.py'
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
