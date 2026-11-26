#!/usr/bin/env bash

docker run -d --name wishlist-test -e SECRET_KEY=test-secret-for-ci -p 5000:5000 wishlist:test
sleep 10
curl -f http://localhost:5000/healthz
docker stop wishlist-test
docker rm wishlist-test
