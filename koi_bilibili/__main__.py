from koi.protocol.stdio import serve_stdio

from koi_bilibili import BilibiliPlugin


def main() -> None:
    serve_stdio(BilibiliPlugin())


if __name__ == "__main__":
    main()
