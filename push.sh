docker build --no-cache  -t mhddos-plus:latest .
docker tag mhddos-plus:latest razor29/mhddos-plus:latest
docker tag mhddos-plus:latest razor29/mhddos-plus:v1.0.5
docker push razor29/mhddos-plus:latest
docker push razor29/mhddos-plus:v1.0.5
