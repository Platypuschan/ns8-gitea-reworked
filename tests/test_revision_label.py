import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


class RevisionLabel(unittest.TestCase):
    def test_module_image_carries_the_built_commit(self):
        # The promotion binds the tested digest to the commit through this label
        script = (ROOT / "build-images.sh").read_text()
        self.assertRegex(script, r'--label="org\.opencontainers\.image\.revision=\$\{GITHUB_SHA:-')

    def test_promotion_reads_the_same_label(self):
        helper = (ROOT / ".github/scripts/promote-tested-image").read_text()
        self.assertIn("org.opencontainers.image.revision", helper)


if __name__ == "__main__":
    unittest.main()
