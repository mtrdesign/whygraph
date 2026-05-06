from mcp.server.fastmcp import FastMCP

mcp = FastMCP("whygraph")


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
