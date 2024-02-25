# Kafka commands
- Start Console Producer
```sh
kafka-console-producer --bootstrap-server localhost:9092 --topic first_topic
```
- Start Console Consumer
```sh
docker exec -it broker kafka-console-consumer --bootstrap-server localhost:9092 --topic text_stream
```
- List all topics
```sh   
kafka-topics --bootstrap-server localhost:9092 --list
```
