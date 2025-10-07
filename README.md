
```

rm checkpoints.db


curl -q -X POST "http://localhost:8000/message" \
         -H "Content-Type: application/json" \
         -d '{"text": "Hello", "thread_ts": "1"}' | jq .message


curl -X POST "http://localhost:8000/message" \
         -H "Content-Type: application/json" \
         -d '{"text": "Alex", "thread_ts": "1"}' | jq .message


curl -X POST "http://localhost:8000/message" \
         -H "Content-Type: application/json" \
         -d '{"text": "left", "thread_ts": "1"}' | jq .message


