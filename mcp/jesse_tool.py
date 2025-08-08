#!/usr/bin/env python3

import asyncio
import json
import logging
import os
import psycopg2
from psycopg2.extras import RealDictCursor
import pandas as pd
from datetime import datetime, timedelta, timezone
from typing import Dict, Any, Optional, List
from dotenv import load_dotenv

# Setup logging
logger = logging.getLogger(__name__)

class JesseHistoricalTool:
    """MCP Tool for Jesse.ai PostgreSQL historical data"""
    
    def __init__(self):
        load_dotenv()
        
        self.db_config = {
            'host': os.getenv('JESSE_DB_HOST', os.getenv("POSTGRES_HOST", "localhost")),
            'port': int(os.getenv('JESSE_DB_PORT', os.getenv("POSTGRES_PORT", "5432"))),
            'database': os.getenv('JESSE_DB_NAME', os.getenv("POSTGRES_DB", "jesse_db")),
            'user': os.getenv('JESSE_DB_USER', os.getenv("POSTGRES_USER", "jesse")),
            'password': os.getenv('JESSE_DB_PASSWORD', os.getenv("POSTGRES_PASSWORD", "password"))
        }
        
        self.connection = None
        self.initialized = False
        self._connection_tested = False
        self._connection_valid = False

    async def initialize(self):
        """Initialize database connection"""
        logger.info("🔧 Initializing Jesse Historical MCP Tool...")
        
        try:
            # Test database connection
            await self._connect()
            await self._test_connection()
            self.initialized = True
            logger.info("✅ Jesse Historical MCP Tool initialized")
            
        except Exception as e:
            logger.error(f"❌ Failed to initialize Jesse tool: {e}")
            raise

    async def _connect(self):
        """Connect to PostgreSQL database"""
        try:
            self.connection = psycopg2.connect(**self.db_config)
            self.connection.autocommit = True
            logger.debug("Connected to Jesse PostgreSQL database")
            
        except Exception as e:
            logger.error(f"Database connection failed: {e}")
            raise

    async def _test_connection(self):
        """Test database connection and verify tables exist"""
        try:
            cursor = self.connection.cursor()
            
            # Check if candle table exists
            cursor.execute("""
                SELECT EXISTS (
                    SELECT FROM information_schema.tables 
                    WHERE table_schema = 'public' 
                    AND table_name = 'candle'
                );
            """)
            
            table_exists = cursor.fetchone()[0]
            
            if not table_exists:
                logger.warning("⚠️ 'candle' table not found in Jesse database")
            else:
                # Test query to get sample data
                cursor.execute("SELECT COUNT(*) FROM candle LIMIT 1;")
                count = cursor.fetchone()[0]
                logger.info(f"📊 Jesse database connected - {count:,} candle records found")
                
                # Check table structure
                cursor.execute("""
                    SELECT column_name, data_type 
                    FROM information_schema.columns 
                    WHERE table_name = 'candle'
                    ORDER BY ordinal_position;
                """)
                columns = cursor.fetchall()
                logger.info(f"📋 Candle table columns: {columns}")
            
            cursor.close()
            
        except Exception as e:
            logger.error(f"Database test failed: {e}")
            raise

    def _reconnect_if_needed(self):
        """Reconnect to database if connection is lost"""
        try:
            if not self.connection or self.connection.closed:
                logger.warning("Database connection lost, reconnecting...")
                asyncio.create_task(self._connect())
                
        except Exception as e:
            logger.error(f"Reconnection failed: {e}")
            raise

    def _format_symbol(self, symbol: str) -> str:
        """Format symbol for database lookup - use the working format from your code"""
        symbol = symbol.upper().replace("/", "-")
        
        # Common symbol mappings using your working format
        symbol_mapping = {
            "BTC": "BTC-USDT",
            "ETH": "ETH-USDT", 
            "BITCOIN": "BTC-USDT",
            "ETHEREUM": "ETH-USDT",
            "BTCUSDT": "BTC-USDT",
            "ETHUSDT": "ETH-USDT"
        }
        
        return symbol_mapping.get(symbol, symbol)

    def _calculate_percentage_change(self, old_value: float, new_value: float) -> float:
        """Calculate percentage change"""
        if old_value == 0:
            return 0
        return ((new_value - old_value) / old_value) * 100

    def _format_price_change(self, change_pct: float) -> str:
        """Format price change with UP/DOWN indicators"""
        if change_pct > 0:
            return f"<UP>**{change_pct:.2f}%** (increase)</UP>"
        elif change_pct < 0:
            return f"<DOWN>**{abs(change_pct):.2f}%** (decrease)</DOWN>"
        else:
            return f"**{change_pct:.2f}%** (no change)"

    def get_historical_prices(self, symbol: str, days_back: int = 365, 
                            exchange: str = 'Binance Perpetual Futures',
                            timeframe: str = '1D') -> List[Dict]:
        """
        Get historical prices using the working logic from your second document
        """
        if not self.connection:
            return []
        
        try:
            # Use your working symbol format
            jesse_symbol = self._format_symbol(symbol)
            
            # Calculate date range with timezone awareness
            end_date = datetime.now(timezone.utc)
            start_date = end_date - timedelta(days=days_back)
            
            logger.info(f"Querying for symbol: {jesse_symbol}, exchange: {exchange}")
            logger.info(f"Date range: {start_date} to {end_date}")
            
            with self.connection.cursor(cursor_factory=RealDictCursor) as cursor:
                # First check what data we have - using your working approach
                cursor.execute("""
                    SELECT COUNT(*) as count,
                           MIN(to_timestamp(timestamp/1000)) as min_date,
                           MAX(to_timestamp(timestamp/1000)) as max_date
                    FROM candle 
                    WHERE symbol = %s AND exchange = %s
                """, (jesse_symbol, exchange))
                
                check_result = cursor.fetchone()
                logger.info(f"Data check - Count: {check_result['count']}")
                
                if not check_result['count']:
                    logger.warning(f"No data found for {jesse_symbol} on {exchange}")
                    return []

                # Use your working query approach - without timeframe filter initially
                query = """
                SELECT 
                    DATE(to_timestamp(timestamp/1000)) as trade_date,
                    timestamp,
                    open::float as open,
                    high::float as high,
                    low::float as low,
                    close::float as close,
                    volume::float as volume
                FROM candle 
                WHERE symbol = %s 
                AND exchange = %s
                AND to_timestamp(timestamp/1000) >= %s
                AND to_timestamp(timestamp/1000) <= %s
                ORDER BY timestamp ASC
                """
                
                cursor.execute(query, (jesse_symbol, exchange, start_date, end_date))
                results = cursor.fetchall()
                logger.info(f"Retrieved {len(results)} raw candles for {jesse_symbol}")
                
                if not results:
                    return []
                
                # Aggregate to daily using your working logic
                daily_data = {}
                for row in results:
                    date_str = row['trade_date'].strftime('%Y-%m-%d')
                    
                    if date_str not in daily_data:
                        daily_data[date_str] = {
                            'date': date_str,
                            'open': row['open'],
                            'high': row['high'],
                            'low': row['low'],
                            'close': row['close'],
                            'volume': row['volume']
                        }
                    else:
                        # Update high/low and close (keep first open)
                        daily_data[date_str]['high'] = max(daily_data[date_str]['high'], row['high'])
                        daily_data[date_str]['low'] = min(daily_data[date_str]['low'], row['low'])
                        daily_data[date_str]['close'] = row['close']  # Last close of the day
                        daily_data[date_str]['volume'] += row['volume']
                
                # Convert to list and sort by date
                candles = list(daily_data.values())
                candles.sort(key=lambda x: x['date'])
                
                logger.info(f"Aggregated to {len(candles)} daily candles")
                return candles
                
        except Exception as e:
            logger.error(f"Error fetching historical data for {symbol}: {e}")
            return []

    def calculate_technical_indicators(self, candles: List[Dict]) -> Dict:
        """Calculate basic technical indicators using your working logic"""
        if len(candles) < 20:
            return {}
        
        closes = [c['close'] for c in candles]
        
        # Simple Moving Averages
        sma_20 = sum(closes[-20:]) / 20
        sma_50 = sum(closes[-50:]) / 50 if len(closes) >= 50 else None
        sma_200 = sum(closes[-200:]) / 200 if len(closes) >= 200 else None
        
        # Calculate volatility
        returns = []
        for i in range(1, len(closes)):
            returns.append((closes[i] - closes[i-1]) / closes[i-1])
        
        volatility = None
        if len(returns) > 1:
            mean_return = sum(returns) / len(returns)
            variance = sum((r - mean_return) ** 2 for r in returns) / len(returns)
            volatility = variance ** 0.5
        
        return {
            'sma_20': round(sma_20, 2),
            'sma_50': round(sma_50, 2) if sma_50 else None,
            'sma_200': round(sma_200, 2) if sma_200 else None,
            'volatility_20d': round(volatility * 100, 2) if volatility else None
        }

    # === MCP Tool Methods - Updated to use working logic ===

    async def get_historical_analysis(self, symbol: str, timeframe: str = "1D") -> str:
        """Get historical price analysis using the working database logic"""
        try:
            self._reconnect_if_needed()
            
            # Use the working approach from your second document
            candles = self.get_historical_prices(symbol, days_back=400, exchange='Binance Perpetual Futures')
            
            if not candles:
                return f"❌ No historical data found for {symbol}"
            
            current_price = candles[-1]['close']
            
            def get_price_n_days_ago(days: int) -> Optional[float]:
                target_index = len(candles) - days - 1
                return candles[target_index]['close'] if target_index >= 0 else None
            
            def calculate_change(old_price: float, new_price: float) -> Dict:
                if old_price == 0:
                    return {"change": 0, "percentage": 0, "direction": "NEUTRAL"}
                
                change = new_price - old_price
                percentage = (change / old_price) * 100
                
                return {
                    "change": round(change, 4),
                    "percentage": round(percentage, 2),
                    "direction": "UP" if change > 0 else "DOWN" if change < 0 else "NEUTRAL",
                    "old_price": round(old_price, 4),
                    "new_price": round(new_price, 4)
                }
            
            # Time periods for analysis
            periods = {
                "1_day": 1,
                "7_days": 7,
                "30_days": 30,
                "90_days": 90,
                "1_year": 365
            }
            
            # Format symbol name
            symbol_name = symbol.split('/')[0] if '/' in symbol else symbol.split('-')[0]
            crypto_names = {
                'BTC': 'Bitcoin (BTC)',
                'ETH': 'Ethereum (ETH)', 
                'ETC': 'Ethereum Classic (ETC)',
                'ADA': 'Cardano (ADA)',
                'DOT': 'Polkadot (DOT)',
                'LINK': 'Chainlink (LINK)'
            }
            full_name = crypto_names.get(symbol_name, f"{symbol_name}")
            
            analysis_result = f"📊 **Price Analysis for {full_name}**\n"
            analysis_result += f"💰 Current Price: ${current_price:,.4f}\n\n"
            
            # Price changes using your working logic
            analysis_result += "📈 **Price Changes:**\n"
            
            period_labels = {
                "1_day": "24 hours",
                "7_days": "7 days",
                "30_days": "30 days",
                "90_days": "90 days",
                "1_year": "1 year"
            }
            
            for period_key, period_label in period_labels.items():
                if period_key in periods:
                    days = periods[period_key]
                    old_price = get_price_n_days_ago(days)
                    if old_price:
                        change_data = calculate_change(old_price, current_price)
                        direction = change_data['direction']
                        percentage = change_data['percentage']
                        
                        if direction == "UP":
                            analysis_result += f"  • **{period_label}**: {self._format_price_change(percentage)}\n"
                        elif direction == "DOWN":
                            analysis_result += f"  • **{period_label}**: {self._format_price_change(percentage)}\n"
                        else:
                            analysis_result += f"  • **{period_label}**: **{percentage:.2f}%** (no change)\n"
            
            # Technical indicators
            technical_indicators = self.calculate_technical_indicators(candles)
            if technical_indicators:
                analysis_result += "\n🔧 **Technical Indicators:**\n"
                
                if technical_indicators.get('sma_20'):
                    analysis_result += f"  • SMA 20: **${technical_indicators['sma_20']:,.2f}**\n"
                if technical_indicators.get('sma_50'):
                    analysis_result += f"  • SMA 50: **${technical_indicators['sma_50']:,.2f}**\n"
                if technical_indicators.get('volatility_20d'):
                    analysis_result += f"  • 20-day Volatility: **{technical_indicators['volatility_20d']:.2f}%**\n"
            
            # Data info
            analysis_result += f"\n📋 **Data Info:**\n"
            analysis_result += f"  • Analysis Period: **{len(candles)}** days\n"
            analysis_result += f"  • Last Updated: **{candles[-1]['date']}**\n"
            
            return analysis_result
            
        except Exception as e:
            logger.error(f"Historical analysis error: {e}")
            return f"Error getting historical analysis for {symbol}: {str(e)}"

    async def get_historical_data_range(self, symbol: str, start_date: str, end_date: str, timeframe: str = "1D") -> str:
        """Get historical data for specific date range using working logic"""
        try:
            self._reconnect_if_needed()
            
            # Parse dates
            start_dt = datetime.strptime(start_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            end_dt = datetime.strptime(end_date, "%Y-%m-%d").replace(tzinfo=timezone.utc) + timedelta(days=1) - timedelta(seconds=1)
            
            jesse_symbol = self._format_symbol(symbol)
            exchange = 'Binance Perpetual Futures'
            
            with self.connection.cursor(cursor_factory=RealDictCursor) as cursor:
                query = """
                SELECT 
                    DATE(to_timestamp(timestamp/1000)) as trade_date,
                    timestamp,
                    open::float as open,
                    high::float as high,
                    low::float as low,
                    close::float as close,
                    volume::float as volume
                FROM candle 
                WHERE symbol = %s 
                AND exchange = %s
                AND to_timestamp(timestamp/1000) >= %s
                AND to_timestamp(timestamp/1000) <= %s
                ORDER BY timestamp ASC
                """
                
                cursor.execute(query, (jesse_symbol, exchange, start_dt, end_dt))
                results = cursor.fetchall()
                
                if not results:
                    return f"❌ No data found for {symbol} between {start_date} and {end_date}"
                
                # Aggregate to daily data using working logic
                daily_data = {}
                for row in results:
                    date_str = row['trade_date'].strftime('%Y-%m-%d')
                    
                    if date_str not in daily_data:
                        daily_data[date_str] = {
                            'date': date_str,
                            'open': row['open'],
                            'high': row['high'],
                            'low': row['low'],
                            'close': row['close'],
                            'volume': row['volume']
                        }
                    else:
                        daily_data[date_str]['high'] = max(daily_data[date_str]['high'], row['high'])
                        daily_data[date_str]['low'] = min(daily_data[date_str]['low'], row['low'])
                        daily_data[date_str]['close'] = row['close']
                        daily_data[date_str]['volume'] += row['volume']
                
                candles = sorted(daily_data.values(), key=lambda x: x['date'])
                
                if not candles:
                    return f"❌ No aggregated data for the requested period"
                
                # Calculate period performance
                start_price = candles[0]['open']
                end_price = candles[-1]['close']
                total_change = ((end_price - start_price) / start_price) * 100
                
                # Find extremes
                highest = max(candles, key=lambda x: x['high'])
                lowest = min(candles, key=lambda x: x['low'])
                
                result = f"📊 **{symbol} Analysis: {start_date} to {end_date}**\n\n"
                result += f"💰 **Period Performance:**\n"
                result += f"  • Start Price: **${start_price:,.4f}** ({candles[0]['date']})\n"
                result += f"  • End Price: **${end_price:,.4f}** ({candles[-1]['date']})\n"
                result += f"  • Total Change: {self._format_price_change(total_change)}\n\n"
                
                result += f"🎯 **Period Extremes:**\n"
                result += f"  • Highest: **${highest['high']:,.4f}** ({highest['date']})\n"
                result += f"  • Lowest: **${lowest['low']:,.4f}** ({lowest['date']})\n\n"
                
                result += f"📋 **Data Summary:**\n"
                result += f"  • Trading Days: **{len(candles)}**\n"
                result += f"  • Total Volume: **{sum(c['volume'] for c in candles):,.0f}**\n"
                
                return result
            
        except Exception as e:
            logger.error(f"Date range analysis error: {e}")
            return f"Error getting data for {symbol} ({start_date} to {end_date}): {str(e)}"

    async def get_raw_historical_data(self, symbol: str, timeframe: str = "1D", limit: int = 100) -> str:
        """Get raw OHLCV data using working logic"""
        try:
            self._reconnect_if_needed()
            
            # Use working approach - get recent data
            candles = self.get_historical_prices(symbol, days_back=limit, exchange='Binance Perpetual Futures')
            
            if not candles:
                return f"❌ No raw data found for {symbol}"
            
            # Take the most recent 'limit' candles
            recent_candles = candles[-limit:] if len(candles) > limit else candles
            
            result = f"📊 **{symbol} Raw Data ({timeframe}, {len(recent_candles)} candles):**\n\n"
            result += f"```json\n{json.dumps(recent_candles, indent=2)}\n```"
            
            return result
            
        except Exception as e:
            logger.error(f"Raw data error: {e}")
            return f"Error getting raw data for {symbol}: {str(e)}"

    async def get_database_status(self) -> str:
        """Get database connection and data status"""
        try:
            self._reconnect_if_needed()
            
            cursor = self.connection.cursor()
            
            # Get database stats
            cursor.execute("SELECT COUNT(*) FROM candle;")
            total_candles = cursor.fetchone()[0]
            
            cursor.execute("SELECT COUNT(DISTINCT symbol) FROM candle;")
            total_symbols = cursor.fetchone()[0]
            
            cursor.execute("SELECT MIN(to_timestamp(timestamp/1000)), MAX(to_timestamp(timestamp/1000)) FROM candle;")
            date_range = cursor.fetchone()
            
            cursor.execute("""
                SELECT symbol, COUNT(*) as count 
                FROM candle 
                GROUP BY symbol 
                ORDER BY count DESC 
                LIMIT 10;
            """)
            top_symbols = cursor.fetchall()
            
            result = f"🗄️ **Jesse Database Status:**\n\n"
            result += f"**Connection:** ✅ Connected\n"
            result += f"**Total Candles:** {total_candles:,}\n"
            result += f"**Total Symbols:** {total_symbols:,}\n"
            
            if date_range[0] and date_range[1]:
                result += f"**Date Range:** {date_range[0]} to {date_range[1]}\n\n"
            
            result += f"**Top Symbols:**\n"
            for symbol, count in top_symbols:
                result += f"• {symbol}: {count:,} candles\n"
            
            cursor.close()
            
            return result
            
        except Exception as e:
            logger.error(f"Database status error: {e}")
            return f"Database status error: {str(e)}"

    async def close(self):
        """Close database connection"""
        logger.info("🔌 Closing Jesse Historical MCP Tool...")
        
        if self.connection and not self.connection.closed:
            self.connection.close()
            
        self.initialized = False
        
        logger.info("✅ Jesse Historical MCP Tool closed")