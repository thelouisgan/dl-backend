#!/usr/bin/env python3
"""
Debug script to check what data exists in Jesse database
"""
import psycopg2
from psycopg2.extras import RealDictCursor
import os
from dotenv import load_dotenv

load_dotenv()

def debug_jesse_data():
    """Debug Jesse database content"""
    
    db_config = {
        'host': os.getenv('JESSE_DB_HOST', 'localhost'),
        'port': int(os.getenv('JESSE_DB_PORT', 5432)),
        'database': os.getenv('JESSE_DB_NAME', 'jesse_db'),
        'user': os.getenv('JESSE_DB_USER', 'jesse_user'),
        'password': os.getenv('JESSE_DB_PASSWORD', 'password')
    }
    
    try:
        conn = psycopg2.connect(**db_config)
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        
        print("🔍 Jesse Database Debug Information")
        print("=" * 50)
        
        # 1. Check available tables
        cursor.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = 'public';")
        tables = [row['table_name'] for row in cursor.fetchall()]
        print(f"📋 Available tables: {tables}")
        
        # 2. Check candle table structure
        if 'candle' in tables:
            print(f"\n📊 Candle table structure:")
            cursor.execute("""
                SELECT column_name, data_type, is_nullable
                FROM information_schema.columns 
                WHERE table_name = 'candle'
                ORDER BY ordinal_position;
            """)
            columns = cursor.fetchall()
            for col in columns:
                print(f"  - {col['column_name']}: {col['data_type']} ({'NULL' if col['is_nullable'] == 'YES' else 'NOT NULL'})")
        
        # 3. Check what data exists
        if 'candle' in tables:
            print(f"\n📈 Data in candle table:")
            
            # Count total records
            cursor.execute("SELECT COUNT(*) as count FROM candle;")
            total_count = cursor.fetchone()['count']
            print(f"  Total records: {total_count:,}")
            
            if total_count > 0:
                # Check available symbols and exchanges
                cursor.execute("SELECT DISTINCT symbol, exchange, COUNT(*) as count FROM candle GROUP BY symbol, exchange;")
                symbol_data = cursor.fetchall()
                
                print(f"  Available trading pairs:")
                for row in symbol_data:
                    print(f"    - {row['symbol']} on {row['exchange']}: {row['count']:,} candles")
                
                # Check date range
                cursor.execute("""
                    SELECT 
                        MIN(to_timestamp(timestamp/1000)) as earliest,
                        MAX(to_timestamp(timestamp/1000)) as latest
                    FROM candle;
                """)
                date_range = cursor.fetchone()
                print(f"  Date range: {date_range['earliest']} to {date_range['latest']}")
                
                # Show sample data
                print(f"\n📊 Sample data (first 3 records):")
                cursor.execute("""
                    SELECT 
                        symbol,
                        exchange,
                        to_timestamp(timestamp/1000) as date_time,
                        open,
                        high,
                        low,
                        close,
                        volume
                    FROM candle 
                    ORDER BY timestamp ASC 
                    LIMIT 3;
                """)
                samples = cursor.fetchall()
                for sample in samples:
                    print(f"    {sample['symbol']} ({sample['exchange']}) @ {sample['date_time']}")
                    print(f"      OHLCV: {sample['open']}, {sample['high']}, {sample['low']}, {sample['close']}, {sample['volume']}")
        else:
            print("❌ No 'candle' table found")
        
        # 4. Test a simple query
        if 'candle' in tables and total_count > 0:
            print(f"\n🧪 Testing simple query for BTC-USDT...")
            cursor.execute("""
                SELECT COUNT(*) as count 
                FROM candle 
                WHERE symbol = 'BTC-USDT';
            """)
            btc_count = cursor.fetchone()['count']
            print(f"  BTC-USDT records: {btc_count:,}")
            
            # Try different exchange names
            exchange_variations = ['Binance', 'Binance Perpetual Futures', 'binance', 'BINANCE']
            for exchange in exchange_variations:
                cursor.execute("""
                    SELECT COUNT(*) as count 
                    FROM candle 
                    WHERE symbol = 'BTC-USDT' AND exchange = %s;
                """, (exchange,))
                count = cursor.fetchone()['count']
                if count > 0:
                    print(f"  Found {count:,} records with exchange = '{exchange}'")
        
        conn.close()
        print(f"\n✅ Debug completed successfully!")
        
    except Exception as e:
        print(f"❌ Error during debug: {e}")

if __name__ == "__main__":
    debug_jesse_data()