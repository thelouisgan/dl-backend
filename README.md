# dl-backend
Real-time AI chatbot streaming interface using FastAPI backend with LangGraph tools

[![wakatime](https://wakatime.com/badge/user/83ae0aa5-9522-4183-a8b6-5598421c5b4f/project/f0dd206c-2f46-4b1b-86d9-896c7c69c004.svg)](https://wakatime.com/badge/user/83ae0aa5-9522-4183-a8b6-5598421c5b4f/project/f0dd206c-2f46-4b1b-86d9-896c7c69c004)

## Integration with Jesse.Trade
### Retrieving historical data from local PostgreSQL database using Docker containers

Demo:
<img width="944" height="954" alt="Screenshot 2025-08-06 at 12 09 10 PM" src="https://github.com/user-attachments/assets/a6e36a9d-f0de-494c-b350-ef5adcdeff1e" />

Python backend retrieves actual data from local PostgreSQL database
```
INFO:     127.0.0.1:52352 - "GET /ask?user_input=Compare%20using%20a%20historical%20analysis%20of%20ETH%20and%20BTC%20over%20the%20past%20year HTTP/1.1" 200 OK
INFO:httpx:HTTP Request: POST https://rsp-ai-foundry-2.cognitiveservices.azure.com/openai/deployments/gpt-4.1/chat/completions?api-version=2025-01-01-preview "HTTP/1.1 200 OK"
INFO:jesse_tools:Querying for symbol: BTC-USDT, exchange: Binance Perpetual Futures, timeframe: 1m
INFO:jesse_tools:Date range: 2024-07-02 03:58:01.687540+00:00 to 2025-08-06 03:58:01.687540+00:00
INFO:jesse_tools:Data check - Count: 2157258, Date range: 2021-06-30 00:00:00+00:00 to 2025-08-06 02:17:00+00:00
INFO:jesse_tools:Available timeframes: 1m
INFO:jesse_tools:Adjusted end date to data availability: 2025-08-06 02:17:00+00:00
INFO:jesse_tools:Retrieved 575899 raw candles for BTC-USDT
INFO:jesse_tools:Aggregated to 401 daily candles
INFO:jesse_tools:Querying for symbol: ETH-USDT, exchange: Binance Perpetual Futures, timeframe: 1m
INFO:jesse_tools:Date range: 2024-07-02 03:58:08.360164+00:00 to 2025-08-06 03:58:08.360164+00:00
INFO:jesse_tools:Data check - Count: 2416550, Date range: 2021-01-01 00:00:00+00:00 to 2025-08-06 03:49:00+00:00
INFO:jesse_tools:Available timeframes: 1m
INFO:jesse_tools:Adjusted end date to data availability: 2025-08-06 03:49:00+00:00
INFO:jesse_tools:Retrieved 575991 raw candles for ETH-USDT
INFO:jesse_tools:Aggregated to 401 daily candles
INFO:httpx:HTTP Request: POST https://rsp-ai-foundry-2.cognitiveservices.azure.com/openai/deployments/gpt-4.1/chat/completions?api-version=2025-01-01-preview "HTTP/1.1 200 OK"
```
