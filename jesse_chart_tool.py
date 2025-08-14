#!/usr/bin/env python3

import asyncio
import json
import logging
import os
import psycopg2
from psycopg2.extras import RealDictCursor
from datetime import datetime, timedelta, timezone
from typing import Dict, Any, Optional, List
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

class JesseChartTool:
    """Enhanced MCP Tool for Jesse.ai chart generation with EXACT chart-renderer.tsx compatibility"""
    
    def __init__(self):
        load_dotenv()
        
        self.db_config = {
            'host': os.getenv('JESSE_DB_HOST', os.getenv("POSTGRES_HOST", "192.168.0.28")),
            'port': int(os.getenv('JESSE_DB_PORT', os.getenv("POSTGRES_PORT", "5432"))),
            'database': os.getenv('JESSE_DB_NAME', os.getenv("POSTGRES_DB", "jesse_db")),
            'user': os.getenv('JESSE_DB_USER', os.getenv("POSTGRES_USER", "jesse")),
            'password': os.getenv('JESSE_DB_PASSWORD', os.getenv("POSTGRES_PASSWORD", "password"))
        }
        
        self.connection = None
        self.initialized = False

    async def initialize(self):
        """Initialize database connection"""
        logger.info("🔧 Initializing Jesse Chart Tool...")
        
        try:
            await self._connect()
            await self._test_connection()
            self.initialized = True
            logger.info("✅ Jesse Chart Tool initialized")
            
        except Exception as e:
            logger.error(f"❌ Failed to initialize Jesse Chart tool: {e}")
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
        """Test database connection"""
        try:
            cursor = self.connection.cursor()
            cursor.execute("SELECT COUNT(*) FROM candle LIMIT 1;")
            count = cursor.fetchone()[0]
            logger.info(f"📊 Jesse Chart database connected - {count:,} candle records found")
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
        """Format symbol for database lookup"""
        symbol = symbol.upper().replace("/", "-")
    
        symbol_mapping = {
            "BTC": "BTC-USDT",
            "ETH": "ETH-USDT", 
            "BITCOIN": "BTC-USDT",
            "ETHEREUM": "ETH-USDT",
            "BTCUSDT": "BTC-USDT",
            "ETHUSDT": "ETH-USDT",
        # Add more mappings for ETH variations
            "ETHER": "ETH-USDT",
            "ETH/USDT": "ETH-USDT",
            "ETH-USD": "ETH-USDT"
        }
    
        return symbol_mapping.get(symbol, symbol)

    def _get_symbol_display_name(self, symbol: str) -> str:
        """Get display name for symbol"""
        base_symbol = symbol.split('-')[0] if '-' in symbol else symbol.split('/')[0] if '/' in symbol else symbol
    
        crypto_names = {
            'BTC': 'Bitcoin',
            'ETH': 'Ethereum',  # Add Ethereum
            'ETC': 'Ethereum Classic',
            'ADA': 'Cardano',
            'DOT': 'Polkadot',
            'LINK': 'Chainlink',
            'SOL': 'Solana',
            'MATIC': 'Polygon',
            'AVAX': 'Avalanche'
        }
    
        return crypto_names.get(base_symbol, base_symbol)
    


    def _get_symbol_color(self, symbol: str) -> str:
        """Get color for symbol charts"""
        base_symbol = symbol.split('-')[0] if '-' in symbol else symbol.split('/')[0] if '/' in symbol else symbol
        
        color_mapping = {
            'BTC': '#F7931A',  # Bitcoin orange
            'ETH': '#627EEA',  # Ethereum blue
            'ETC': '#328332',  # Ethereum Classic green
            'ADA': '#0033AD',  # Cardano blue
            'DOT': '#E6007A',  # Polkadot pink
            'LINK': '#2A5ADA', # Chainlink blue
            'SOL': '#9945FF',  # Solana purple
            'MATIC': '#8247E5', # Polygon purple
            'AVAX': '#E84142'  # Avalanche red
        }
        
        return color_mapping.get(base_symbol, '#2E86AB')  # Default blue

    def get_historical_prices(self, symbol: str, days_back: int = 365, 
                            exchange: str = 'Binance Perpetual Futures') -> List[Dict]:
        """Get historical prices - FIXED datetime issue"""
        if not self.connection:
            logger.error("No database connection available")
            raise Exception("Database connection not available")
        
        try:
            jesse_symbol = self._format_symbol(symbol)
            
            end_date = datetime.now(timezone.utc)
            start_date = end_date - timedelta(days=days_back)
            
            logger.info(f"Querying chart data for symbol: {jesse_symbol}, exchange: {exchange}")
            logger.info(f"Date range: {start_date} to {end_date}")
            
            with self.connection.cursor(cursor_factory=RealDictCursor) as cursor:
                # Check data availability first
                cursor.execute("""
                    SELECT COUNT(*) as count
                    FROM candle 
                    WHERE symbol = %s AND exchange = %s
                """, (jesse_symbol, exchange))
                
                check_result = cursor.fetchone()
                logger.info(f"Chart data check - Count: {check_result['count']}")
                
                if not check_result['count']:
                    logger.error(f"No chart data found for {jesse_symbol} on {exchange}")
                    raise Exception(f"No chart data found for {jesse_symbol} on {exchange}")

                # Get raw candle data
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
                logger.info(f"Retrieved {len(results)} raw candles for chart")
                
                if not results:
                    logger.error(f"No results returned from query for {jesse_symbol}")
                    raise Exception(f"No results returned from query for {jesse_symbol}")
                
                # Aggregate to daily - FIXED: row['trade_date'] is already a date object
                daily_data = {}
                for row in results:
                    # FIX: Don't call .date() on date object
                    if isinstance(row['trade_date'], datetime):
                        date_str = row['trade_date'].date().isoformat()
                    else:
                        date_str = row['trade_date'].isoformat()  # Already a date object
                    
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
                
                candles = list(daily_data.values())
                candles.sort(key=lambda x: x['date'])
                
                logger.info(f"Aggregated to {len(candles)} daily candles for chart")
                return candles
                
        except Exception as e:
            logger.error(f"Error fetching chart data for {symbol}: {e}")
            import traceback
            traceback.print_exc()
            raise Exception(f"Failed to fetch chart data for {symbol}: {str(e)}")

    async def get_price_chart_data(self, symbol: str, days_back: int = 30, timeframe: str = "1D") -> str:
        """Get price chart data in chart-renderer.tsx compatible format"""
        try:
            self._reconnect_if_needed()
            
            logger.info(f"🎯 Generating chart-renderer.tsx compatible data for {symbol}, {days_back} days")
            
            # Get real data from database
            candles = self.get_historical_prices(symbol, days_back=days_back, exchange='Binance Perpetual Futures')
            
            if not candles or len(candles) == 0:
                error_msg = f"No data available for {symbol}"
                logger.error(error_msg)
                raise Exception(error_msg)
            
            # Build chart data in the EXACT format chart-renderer.tsx expects
            symbol_name = self._get_symbol_display_name(symbol)
            symbol_color = self._get_symbol_color(symbol)
            
            # Ensure clean data arrays
            dates = [candle['date'] for candle in candles]
            prices = [float(candle['close']) for candle in candles]
            
            # Validate data before creating chart
            if len(dates) == 0 or len(prices) == 0:
                error_msg = "Empty data arrays after processing"
                logger.error(error_msg)
                raise Exception(error_msg)
            
            if len(dates) != len(prices):
                logger.warning(f"Data length mismatch: dates={len(dates)}, prices={len(prices)}")
                # Trim to shortest length
                min_length = min(len(dates), len(prices))
                dates = dates[:min_length]
                prices = prices[:min_length]
            
            # EXACT structure that chart-renderer.tsx expects
            chart_data = {
                "history": {
                    "title": f"Last {days_back} Days",
                    "xlabel": "Date",
                    "content": [{
                        "name": symbol_name,
                        "primary_colour": symbol_color,
                        "x": dates,
                        "price": {
                            "y": prices,
                            "ylabel": "Price (USD)"
                        }
                    }]
                }
            }
            
            logger.info(f"✅ Chart-renderer.tsx compatible data created for {symbol} with {len(candles)} data points")
            logger.info(f"📊 Date range: {dates[0]} to {dates[-1]}")
            logger.info(f"💰 Price range: ${prices[0]:.2f} to ${prices[-1]:.2f}")
            
            result = json.dumps(chart_data, indent=2)
            
            # Console output before sending to frontend
            print(f"\n🎯 CHART DATA OUTPUT FOR {symbol} (chart-renderer.tsx compatible):")
            print("=" * 70)
            print(result)
            print("=" * 70)
            
            return result
            
        except Exception as e:
            logger.error(f"Chart data error for {symbol}: {e}")
            import traceback
            traceback.print_exc()
            raise Exception(f"Failed to generate chart data for {symbol}: {str(e)}")




    async def get_comparison_chart_data(self, symbols: List[str], days_back: int = 90) -> str:
        """Get comparison chart data for multiple symbols - SINGLE CHART with MULTIPLE LINES"""
        try:
            self._reconnect_if_needed()
        
            logger.info(f"🔊 Generating SINGLE CHART with MULTIPLE LINES for {symbols}, {days_back} days")
        
            if len(symbols) > 5:
                error_msg = "Too many symbols requested. Maximum 5 symbols allowed."
                logger.error(error_msg)
                raise Exception(error_msg)
        
            content_list = []
            successful_symbols = []
            
            # FIXED: Collect data for each symbol independently
            symbol_data_dict = {}
            
            for symbol in symbols:
                try:
                    candles = self.get_historical_prices(symbol, days_back=days_back, exchange='Binance Perpetual Futures')
                
                    if candles and len(candles) > 0:
                        symbol_data_dict[symbol] = candles
                        logger.info(f"✅ Collected {len(candles)} data points for {symbol}")
                    else:
                        logger.warning(f"❌ No data for {symbol} in comparison")
                    
                except Exception as symbol_error:
                    logger.error(f"Error processing {symbol} for comparison: {symbol_error}")
                    continue
            
            if not symbol_data_dict:
                error_msg = f"No data available for any of the requested symbols: {symbols}"
                logger.error(error_msg)
                raise Exception(error_msg)
            
            # FIXED: Find the common date range across ALL symbols
            all_dates_sets = []
            for symbol, candles in symbol_data_dict.items():
                symbol_dates = set(candle['date'] for candle in candles)
                all_dates_sets.append(symbol_dates)
            
            # Get intersection of all date sets (dates that exist for ALL symbols)
            common_dates = set.intersection(*all_dates_sets) if all_dates_sets else set()
            
            if not common_dates:
                logger.warning("No common dates found, using union of all dates")
                # Fallback: use union of all dates
                common_dates = set.union(*all_dates_sets) if all_dates_sets else set()
            
            sorted_common_dates = sorted(list(common_dates))
            logger.info(f"📅 Common date range: {sorted_common_dates[0]} to {sorted_common_dates[-1]} ({len(sorted_common_dates)} days)")
            
            # FIXED: Process each symbol for the comparison chart
            for symbol in symbol_data_dict.keys():
                try:
                    candles = symbol_data_dict[symbol]
                    symbol_name = self._get_symbol_display_name(symbol)
                    symbol_color = self._get_symbol_color(symbol)
                
                    # Create a date-indexed dictionary for this symbol
                    candle_dict = {candle['date']: candle for candle in candles}
                
                    # FIXED: For comparison, use actual prices instead of percentage changes
                    # This ensures both lines show up properly
                    prices = []
                    valid_dates = []
                    
                    for date in sorted_common_dates:
                        if date in candle_dict:
                            current_price = float(candle_dict[date]['close'])
                            prices.append(round(current_price, 2))
                            valid_dates.append(date)
                    
                    # FIXED: Only add symbols that have actual data
                    if len(prices) > 0 and len(valid_dates) > 0:
                        # Add this symbol as a separate line on the SAME chart
                        content_item = {
                            "name": f"{symbol_name}",
                            "primary_colour": symbol_color,  # Each line gets its own color
                            "x": valid_dates,
                            "price": {
                                "y": prices,
                                "ylabel": "Price (USD)"
                            }
                        }
                        
                        content_list.append(content_item)
                        successful_symbols.append(symbol)
                        
                        logger.info(f"✅ Added {symbol} as line #{len(content_list)} with {len(prices)} points (color: {symbol_color})")
                        logger.info(f"   First price: ${prices[0]}, Last price: ${prices[-1]}")
                    else:
                        logger.warning(f"❌ No valid prices for {symbol}")
                    
                except Exception as symbol_error:
                    logger.error(f"Error processing {symbol} data: {symbol_error}")
                    continue
            
            # FIXED: Better error handling
            if not content_list:
                error_msg = f"No valid data available for any of the requested symbols: {symbols}"
                logger.error(error_msg)
                raise Exception(error_msg)
            
            # FIXED: Ensure we have at least the expected number of lines
            if len(content_list) < len(symbols):
                logger.warning(f"Expected {len(symbols)} lines, but only got {len(content_list)}")
                logger.warning(f"Missing symbols: {set(symbols) - set(successful_symbols)}")
            
            # 🎯 CRITICAL: Create SINGLE chart with MULTIPLE content items (lines)
            chart_data = {
                "history": {
                    "title": f"Comparison ({days_back} Days)",
                    "xlabel": "Date", 
                    "content": content_list  # 🔥 This creates multiple lines on the SAME chart
                }
            }
            
            logger.info(f"✅ SINGLE COMPARISON CHART generated with {len(content_list)} lines:")
            for i, item in enumerate(content_list):
                logger.info(f"  - Line {i+1}: {item['name']} ({item['primary_colour']}) - {len(item['x'])} points")
            
            result = json.dumps(chart_data, indent=2)
            
            # Console output before sending to frontend
            print(f"\n🎯 SINGLE COMPARISON CHART DATA OUTPUT FOR {symbols} (chart-renderer.tsx compatible):")
            print("="*80)
            print(f"📊 Number of lines on single chart: {len(content_list)}")
            for i, item in enumerate(content_list):
                print(f"  Line {i+1}: {item['name']} - Color: {item['primary_colour']} - Points: {len(item.get('x', []))}")
            print("-"*80)
            print(result[:500] + "..." if len(result) > 500 else result)
            print("="*80)
            
            return result
        
        except Exception as e:
            logger.error(f"Comparison chart error: {e}")
            import traceback
            traceback.print_exc()
            raise Exception(f"Failed to generate comparison chart: {str(e)}")
            




    async def get_candlestick_chart_data(self, symbol: str, days_back: int = 30) -> str:
        """Get candlestick chart data - Note: chart-renderer.tsx may need updates for candlestick support"""
        try:
            self._reconnect_if_needed()
            
            logger.info(f"🕯️ Generating candlestick data for {symbol}, {days_back} days")
            
            candles = self.get_historical_prices(symbol, days_back=days_back, exchange='Binance Perpetual Futures')
            
            if not candles or len(candles) == 0:
                error_msg = f"No candlestick data available for {symbol}"
                logger.error(error_msg)
                raise Exception(error_msg)
            
            symbol_name = self._get_symbol_display_name(symbol)
            
            # Format for candlestick chart (you may need to update chart-renderer.tsx for this)
            candlestick_data = []
            for candle in candles:
                candlestick_data.append({
                    "x": candle['date'],
                    "y": [
                        float(candle['open']),
                        float(candle['high']),
                        float(candle['low']),
                        float(candle['close'])
                    ]
                })
            
            chart_data = {
                "candlestick": {
                    "title": f"{symbol_name} Candlestick ({days_back} Days)",
                    "xlabel": "Date",
                    "ylabel": "Price (USD)",
                    "data": candlestick_data
                }
            }
            
            logger.info(f"✅ Candlestick chart created for {symbol} with {len(candles)} candles")
            
            result = json.dumps(chart_data, indent=2)
            
            # Console output before sending to frontend
            print(f"\n🎯 CANDLESTICK CHART DATA OUTPUT FOR {symbol}:")
            print("=" * 55)
            print(result)
            print("=" * 55)
            
            return result
            
        except Exception as e:
            logger.error(f"Candlestick chart error for {symbol}: {e}")
            import traceback
            traceback.print_exc()
            raise Exception(f"Failed to generate candlestick chart for {symbol}: {str(e)}")

    async def close(self):
        """Close database connection"""
        logger.info("🔌 Closing Jesse Chart Tool...")
        
        if self.connection and not self.connection.closed:
            self.connection.close()
            
        self.initialized = False
        
        logger.info("✅ Jesse Chart Tool closed")