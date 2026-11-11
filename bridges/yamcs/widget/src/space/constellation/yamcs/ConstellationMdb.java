package space.constellation.yamcs;

import java.io.IOException;
import java.io.UncheckedIOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardCopyOption;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.util.HexFormat;

import org.yamcs.mdb.XtceLoader;

/**
 * Loads the /ConstellationForecast parameters (constellation.xml, packed in this jar). ForecastPlugin
 * adds them to every instance by itself; naming this loader in an instance's mdb section as well
 * keeps them through a restart of that instance alone:
 *
 * <pre>
 * mdb:
 *   - type: space.constellation.yamcs.ConstellationMdb
 * </pre>
 *
 * <p>XtceLoader only reads files, so the XML is unpacked to a temporary file first.
 */
public class ConstellationMdb extends XtceLoader {

    static final String XML = "constellation.xml";

    public ConstellationMdb() {
        super(unpack().toString());
    }

    private static Path unpack() {
        try (var in = ConstellationMdb.class.getResourceAsStream(XML)) {
            var file = Files.createTempFile("constellation", ".xml");
            file.toFile().deleteOnExit();
            Files.copy(in, file, StandardCopyOption.REPLACE_EXISTING);
            return file;
        } catch (IOException e) {
            throw new UncheckedIOException("Could not unpack " + XML + " from constellation-yamcs.jar", e);
        }
    }

    // XtceLoader names its cache entry by file path and modification time, both new on every
    // start; naming it by content lets Yamcs reuse its serialized database across restarts.
    @Override
    public String getConfigName() {
        try (var in = ConstellationMdb.class.getResourceAsStream(XML)) {
            var sha = MessageDigest.getInstance("SHA-256").digest(in.readAllBytes());
            return "constellation-" + HexFormat.of().formatHex(sha, 0, 8);
        } catch (IOException | NoSuchAlgorithmException e) {
            throw new IllegalStateException(e);
        }
    }

    // Edits would land in the temporary copy and vanish on restart.
    @Override
    public boolean isWritable() {
        return false;
    }
}
