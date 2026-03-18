import requests

def generate_pixel_art():
    api_url = "https://api.groq.com/image-generation"
    payload = {
        "prompt": "Terminal-style pixel art logo/mascot for Arbitrout dashboard with green-on-black theme",
        "style": "pixel art",
        "size": "256x256"
    }
    response = requests.post(api_url, json=payload)
    if response.status_code == 200:
        image_data = response.json()["image"]
        with open("src/static/img/logo.png", "wb") as f:
            f.write(image_data)
        print("Logo generated and saved to src/static/img/logo.png")
    else:
        print("Failed to generate logo")

    # Generate favicon
    payload["size"] = "16x16"
    response = requests.post(api_url, json=payload)
    if response.status_code == 200:
        image_data = response.json()["image"]
        with open("src/static/img/favicon.png", "wb") as f:
            f.write(image_data)
        print("Favicon generated and saved to src/static/img/favicon.png")
    else:
        print("Failed to generate favicon")

    # Generate loading animation sprites
    payload["size"] = "32x32"
    payload["animation"] = True
    response = requests.post(api_url, json=payload)
    if response.status_code == 200:
        image_data = response.json()["image"]
        with open("src/static/img/loading.gif", "wb") as f:
            f.write(image_data)
        print("Loading animation generated and saved to src/static/img/loading.gif")
    else:
        print("Failed to generate loading animation")

if __name__ == "__main__":
    generate_pixel_art()
