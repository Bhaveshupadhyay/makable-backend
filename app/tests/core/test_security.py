import base64

from nacl.public import PrivateKey, SealedBox

from app.core.security import seal_for_github


def test_seal_for_github_only_opens_with_the_repos_private_key() -> None:
    key = PrivateKey.generate()
    public = base64.b64encode(bytes(key.public_key)).decode()

    sealed = seal_for_github(public, "vercel-token")

    assert "vercel-token" not in sealed
    assert SealedBox(key).decrypt(base64.b64decode(sealed)) == b"vercel-token"
