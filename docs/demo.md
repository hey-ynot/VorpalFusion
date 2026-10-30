# Demo

![An astronaut on a jellyfish becomes a pink felt diorama, then comes alive](media/vorpalfusion.gif)

A lunar tea party becomes a pink wool-felt scene, then the camera sweeps around the astronaut and jellyfish.
Made locally with FLUX 3 Klein and FastH3. The reel is trimmed and played faster for a quick preview.

## Try this scene

Start by telling your coding agent:

> Set up VorpalFusion from https://github.com/hey-ynot/VorpalFusion and then open the UI in the browser.

Use seed **308097488**, **4 Live steps**, and auto speed. The image steps use CFG 1 and HD.
Start with one panel, Flow, Lock and Pin off, no layout boxes, and Enrich off. Check these settings after
switching templates. Let each picture settle before moving on.

1. Choose **FLUX 3 Image Klein Distilled: Text to Image**, 1:1, and type
   `A tiny orange astronaut on the moon`.
2. Switch to 16:9 and type
   `A tiny orange astronaut riding a giant translucent jellyfish over the moon, teal aurora, surreal cinematic wide shot`.
3. Set Panels to 4 and replace the words with
   `A tiny orange astronaut rides a translucent jellyfish through a teal aurora above the moon. A miniature lunar tea party drifts beside them: a porcelain teapot, floating croissants, silver teaspoons, and paper lanterns shaped like Saturn. Huge velvet-black sky, playful practical miniature photography, luminous jellyfish tendrils, soft film grain, cinematic rim lighting, no lettering, no watermark. The camera is low and close to the jellyfish while the horizon curves gently behind it.`
   Choose panel 2, let it settle, and download the selected picture.
4. Choose **As reference** and confirm **FLUX 3 Image Klein Distilled: Image Edit** with the selected tea-party
   picture in its source slot. Match the source picture's aspect, return to one panel, and type
   `Transform the scene into a handmade wool-felt stop-motion set. Keep the astronaut, jellyfish, floating tea set and their arrangement. Make the jellyfish bright coral pink with soft glowing tentacles, and turn the sky deep indigo with golden paper stars.`
5. Let the felt picture settle and download it. Choose **FastH3 image to video** and put that picture in the
   **first frame** slot. Confirm the thumbnail shows the pink felt scene. Use HD, 2 seconds, seed 308097488,
   and 4 Live steps. Type
   `The pink felt jellyfish gently pulses and floats upward. Its wool tentacles sway, the tiny astronaut waves one arm, and the floating teacups slowly orbit. Stop-motion handmade movement. Locked camera, no cuts.`
   Wait for the full clip, watch a complete loop, and download it.
6. Keep the same source and seed, change the duration to 5 seconds, and replace the words with
   `The camera makes a sweeping clockwise orbit around the pink felt jellyfish. The astronaut tumbles slowly head over heels, teacups spin like a carousel, and the jellyfish accelerates sideways with its tentacles streaming behind it. Playful stop-motion movement, no cuts.`
   Wait for the full clip, watch a complete loop, then download the animation.

Four Live steps controls the preview; settling and the full clip are separate stages. Save each source picture
before switching templates. These are the recorded prompts and controls; results can vary with model files and
backend state. See [Requirements](../README.md#requirements) for the models used here.
