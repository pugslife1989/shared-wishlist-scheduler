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

    stage('Trivy scan'){
      steps {
         sh 'docker run --rm -v /var/run/docker.sock:/var/run/docker.sock aquasec/trivy image --severity CRITICAL,HIGH --exit-code 1 --vuln-type library wishlist:test'    
      }
    }    

    stage('smoke test'){
      steps {
        sh './smoke-test.sh'
      }
    }

    stage('Deploy') {
      steps {
       sh 'cd /wishlist-scheduler && docker compose up -d --build'
      }
    }

    stage('Health Check'){
      steps {
        sh 'sleep 10 && docker run --rm --network host curlimages/curl -f http://localhost:5000/healthz'
      }
    }
  }
}
