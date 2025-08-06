from fastapi import FastAPI
from fastapi.responses import StreamingResponse
import os
import json
from dotenv import load_dotenv

import ccxt
from langchain_core.tools import tool
from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langchain_openai import AzureChatOpenAI

# Import your Jesse tools
from jesse_tools import (
    get_crypto_historical_analysis, 
    get_crypto_raw_historical_data,
    get_crypto_historical_analysis_date_range
)

# === Load Environment Variables ===
load_dotenv(dotenv_path=os.path.join("config", ".env"))

api_version = os.getenv("AZURE_OPENAI_API_VERSION", "2024-12-01-preview")
endpoint = os.getenv("AZURE_OPENAI_ENDPOINT")
azure_api_key = os.getenv("AZURE_OPENAI_API_KEY")
deployment = os.getenv("AZURE_OPENAI_DEPLOYMENT_NAME", "gpt-4o")

# === Setup FastAPI app ===
app = FastAPI()

# === Setup ccxt exchange ===
exchange = ccxt.binance()

@tool
def get_crypto_price(symbol: str) -> str:
    """
    Get the current price of a cryptocurrency.
    Example symbols: 'BTC/USDT', 'ETH/USDT'
    """
    try:
        ticker = exchange.fetch_ticker(symbol.upper())
        return f"The current price of {symbol.upper()} is {ticker['last']} {symbol.split('/')[1].upper()}."
    except Exception as e:
        return f"Error fetching price for {symbol}: {e}"

@tool
def get_crypto_info(symbol: str) -> str:
    """
    Get detailed ticker info for a cryptocurrency pair.
    Example symbols: 'BTC/USDT', 'ETH/USDT'
    """
    try:
        ticker = exchange.fetch_ticker(symbol.upper())
        return (
            f"Symbol: {symbol.upper()}\n"
            f"Last Price: {ticker['last']}\n"
            f"High (24h): {ticker['high']}\n"
            f"Low (24h): {ticker['low']}\n"
            f"Bid: {ticker['bid']}\n"
            f"Ask: {ticker['ask']}\n"
            f"Volume: {ticker['baseVolume']}"
        )
    except Exception as e:
        return f"Error fetching details for {symbol}: {e}"

# === Streaming API Endpoint ===
@app.get("/ask")
async def ask_stream(user_input: str):
    def generate_response():
        try:
            # Create LLM
            client = AzureChatOpenAI(
                azure_endpoint=endpoint,
                azure_deployment=deployment,
                openai_api_version=api_version,
                api_key=azure_api_key,
            )
            
            # Bind all tools including Jesse tools
            client_with_tools = client.bind_tools([
                get_crypto_info, 
                get_crypto_price,
                get_crypto_historical_analysis,
                get_crypto_historical_analysis_date_range,
                get_crypto_raw_historical_data
            ])
            
            # Enhanced system prompt with correct UP/DOWN tag formatting
            system_prompt = """You are a crypto analysis assistant with access to:
1. Real-time crypto prices (get_crypto_price, get_crypto_info)
2. Historical price analysis from Jesse.ai database (get_crypto_historical_analysis)
3. Date-specific historical analysis (get_crypto_historical_analysis_date_range)
4. Raw historical data (get_crypto_raw_historical_data)

CRITICAL FORMATTING RULES:
- For price increases: Use <UP>**X.XX%** (increase)</UP>
- For price decreases: Use <DOWN>**X.XX%** (decrease)</DOWN>
- NEVER use other formats like "UP +X%" or "DOWN -X%"
- Always include "**" around the percentage and "(increase)" or "(decrease)" text
- Only use UP/DOWN tags for percentage changes, not for other text

CORRECT EXAMPLE:
* 24 hours: <UP>**3.98%** (increase)</UP>
* 7 days: <DOWN>**3.45%** (decrease)</DOWN>

INCORRECT EXAMPLES (DO NOT USE):
* 24 hours: UP +3.98%
* 7 days: DOWN -3.45%
* 24 hours: <UP>+3.98%</UP>

When users ask about:
- Current prices: Use get_crypto_price or get_crypto_info
- Price history/trends/performance: Use get_crypto_historical_analysis (this provides data in emoji format, convert it to UP/DOWN tags)
- Specific date ranges (e.g., "August 2024", "January 2023"): Use get_crypto_historical_analysis_date_range
- Historical data for charts: Use get_crypto_raw_historical_data

For date range requests:
- Parse dates like "August 2024" as "2024-08-01" to "2024-08-31"
- Parse "Q1 2024" as "2024-01-01" to "2024-03-31"
- Parse "January 2023" as "2023-01-01" to "2023-01-31"

Always format your final response with proper UP/DOWN tags for percentage changes."""
            
            # System and user prompt
            messages_with_tools = [
                SystemMessage(system_prompt),
                HumanMessage(user_input),
            ]
            
            # Run LangChain agent to check for tool calls
            result = client_with_tools.invoke(messages_with_tools)
            messages_with_tools.append(result)

            # Process tool calls if any
            if result.tool_calls:
                for tool_call in result.tool_calls:
                    # Emit tool call info - properly escape JSON
                    tool_data = {
                        "type": "tool_call",
                        "id": tool_call["id"],
                        "name": tool_call["name"],
                        "args": tool_call["args"]
                    }
                    yield f'{json.dumps([tool_data])}\n'
                    
                    # Execute tool
                    tool_fn = {
                        "get_crypto_info": get_crypto_info, 
                        "get_crypto_price": get_crypto_price,
                        "get_crypto_historical_analysis": get_crypto_historical_analysis,
                        "get_crypto_historical_analysis_date_range": get_crypto_historical_analysis_date_range,
                        "get_crypto_raw_historical_data": get_crypto_raw_historical_data
                    }[tool_call["name"]]
                    
                    tool_result = tool_fn.invoke(tool_call)
                    tool_message = ToolMessage(
                        content=tool_result,
                        tool_call_id=tool_call["id"]
                    )
                    messages_with_tools.append(tool_message)
            
            # Generate final response with streaming
            message_id = f"msg-{abs(hash(user_input)) % 10000}"
            
            # First, let's collect all streaming chunks to avoid broken JSON
            collected_content = ""
            for chunk in client_with_tools.stream(messages_with_tools):
                if chunk.content:
                    collected_content += chunk.content
                    # Send each character/token individually but as complete JSON
                    chunk_data = {
                        "type": "text",
                        "id": message_id,
                        "data": chunk.content
                    }
                    yield f'{json.dumps([chunk_data])}\n'
                    
        except Exception as e:
            error_msg = f"Error: {str(e)}"
            error_data = {
                "type": "text",
                "id": f"error-{abs(hash(str(e))) % 1000}",
                "data": error_msg
            }
            yield f'{json.dumps([error_data])}\n'
    
    return StreamingResponse(
        generate_response(),
        media_type="text/plain",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        }
    )

# === Non-streaming endpoint for compatibility ===
@app.get("/ask_simple")
async def ask_simple(user_input: str):
    try:
        # Create LLM
        client = AzureChatOpenAI(
            azure_endpoint=endpoint,
            azure_deployment=deployment,
            openai_api_version=api_version,
            api_key=azure_api_key,
        )
        client_with_tools = client.bind_tools([
            get_crypto_info, 
            get_crypto_price,
            get_crypto_historical_analysis,
            get_crypto_historical_analysis_date_range,
            get_crypto_raw_historical_data
        ])
        
        # Enhanced system prompt for simple endpoint too
        system_prompt = """You are a crypto analysis assistant with access to real-time and historical crypto data from Jesse.ai database.

CRITICAL FORMATTING RULES:
- For price increases: Use <UP>**X.XX%** (increase)</UP>
- For price decreases: Use <DOWN>**X.XX%** (decrease)</DOWN>
- NEVER use other formats like "UP +X%" or "DOWN -X%"
- Always include "**" around the percentage and "(increase)" or "(decrease)" text

CORRECT EXAMPLE:
* 24 hours: <UP>**3.98%** (increase)</UP>
* 7 days: <DOWN>**3.45%** (decrease)</DOWN>

When the Jesse tool returns data with <UP> or  <DOWN>, convert those to proper UP/DOWN tags.

For specific date ranges, use get_crypto_historical_analysis_date_range with YYYY-MM-DD format."""
        
        # System and user prompt
        messages_with_tools = [
            SystemMessage(system_prompt),
            HumanMessage(user_input),
        ]
        
        # Run LangChain agent
        result = client_with_tools.invoke(messages_with_tools)
        messages_with_tools.append(result)

        tool_outputs = []
        if result.tool_calls:
            for tool_call in result.tool_calls:
                tool_fn = {
                    "get_crypto_info": get_crypto_info, 
                    "get_crypto_price": get_crypto_price,
                    "get_crypto_historical_analysis": get_crypto_historical_analysis,
                    "get_crypto_historical_analysis_date_range": get_crypto_historical_analysis_date_range,
                    "get_crypto_raw_historical_data": get_crypto_raw_historical_data
                }[tool_call["name"]]
                
                tool_result = tool_fn.invoke(tool_call)
                tool_outputs.append(tool_result)
                
                tool_message = ToolMessage(
                    content=tool_result,
                    tool_call_id=tool_call["id"]
                )
                messages_with_tools.append(tool_message)

        # Final response
        final_response = client_with_tools.invoke(messages_with_tools)
        
        return {
            "response": final_response.content,
            "tool_outputs": tool_outputs
        }
        
    except Exception as e:
        return {
            "error": str(e),
            "response": f"Error: {str(e)}",
            "tool_outputs": []
        }