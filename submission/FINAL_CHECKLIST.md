# Final submission checklist

## Required portal fields

- [x] Scenario identified: DBS DCTA, Scenario 1.
- [x] Project title prepared.
- [x] Blurb is under ten words.
- [x] Project description covers users, pain points, architecture, prompt role,
  business value, and metrics.
- [x] Cover image is 16:9 and exactly 380 × 216 pixels.
- [x] Three CodeBuddy chat screenshots are present.
- [x] Original CodeBuddy conversation export is preserved locally for upload.
- [x] Final demo-video URL added: `https://youtu.be/s2UBePD_5Y0`.
- [ ] Confirm public/viewer access to the demo video.
- [ ] Add a verified live-project URL, or enter N/A.
- [ ] Add team/member details exactly as registered in the hackathon portal.

## Release gate

- [x] Full Python test suite passes from the release tree.
- [x] JavaScript syntax checks pass.
- [ ] Repository boundary scan passes on the committed tree.
- [ ] A clean clone starts in fixture mode without secrets or model downloads.
- [ ] Active `.env` files, service keys, ledgers, model weights, runtime audio,
  private deployment state, and the competition PDF are absent from Git.
- [x] A sanitized squash release is committed on `main`; secret-bearing
  `develop` history is not merged, fast-forwarded, or pushed again.
- [x] The previously exposed inference service key is revoked and replaced.
- [ ] `origin/main` points to the verified release commit.
- [ ] Release source archive and checksums are generated.

## Final human review

- [ ] Watch the uploaded video from its public link, with sound on.
- [ ] Confirm the video is five to eight minutes and starts by naming the
  selected scenario.
- [ ] Confirm the video demonstrates spoken completion after a stock purchase.
- [ ] Open every submitted link in a signed-out/private browser window.
- [ ] Upload `submission/assets/dimon-cover-380x216.png`, not the source image.
- [ ] Upload the three screenshots in chronological order.
- [ ] Submit before the portal closes and save the confirmation receipt.
