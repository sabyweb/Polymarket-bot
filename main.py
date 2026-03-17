from alerts import setup_logger
from bot import MarketMakerBot


def main():
    # Set up logging first — before anything else
    setup_logger()

    # Create and connect the bot
    bot = MarketMakerBot()

    if not bot.connect():
        print("❌  Failed to connect to Polymarket. Check your credentials in .env")
        return

    # Run the bot
    bot.run()


if __name__ == "__main__":
    main()
