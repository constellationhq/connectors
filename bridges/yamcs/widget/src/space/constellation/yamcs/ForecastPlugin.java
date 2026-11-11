package space.constellation.yamcs;

import java.io.IOException;
import java.nio.file.Files;
import java.util.List;
import java.util.Map;

import org.yamcs.Plugin;
import org.yamcs.PluginException;
import org.yamcs.YConfiguration;
import org.yamcs.YamcsServer;
import org.yamcs.mdb.MdbFactory;
import org.yamcs.web.WebPlugin;
import org.yamcs.xtce.SpaceSystem;

/**
 * Adds the Constellation Forecast page to the Yamcs web interface, and the /ConstellationForecast
 * parameters to every instance's mission database, so installing is copying this jar into
 * lib/ext. The page reads what the Constellation bridge writes to the "constellation" bucket,
 * through the operator's own Yamcs session, so it needs no Constellation key and no network path
 * beyond Yamcs.
 *
 * <p>Uses WebPlugin.addExtension, which Yamcs marks experimental (5.14.0).
 */
public class ForecastPlugin implements Plugin {

    static final String PAGE = "constellation-forecast";
    static final String SPACE = "ConstellationForecast";
    // The page and the shared view it draws with (bridges/shared/forecast-view.js).
    static final String[] FILES = {PAGE + ".js", "forecast-view.js"};

    @Override
    public void onLoad(YConfiguration config) throws PluginException {
        var web = YamcsServer.getServer().getPluginManager().getPlugin(WebPlugin.class);
        if (web == null) {
            throw new PluginException("The Constellation Forecast page needs the yamcs-web plugin");
        }
        try {
            var root = Files.createTempDirectory(PAGE);
            for (var file : FILES) {
                try (var in = ForecastPlugin.class.getResourceAsStream("/web/" + file)) {
                    Files.copy(in, root.resolve(file));
                }
            }
            web.addExtension(PAGE, Map.of("bucket", "constellation"), root);
        } catch (IOException e) {
            throw new PluginException("Could not unpack the Constellation Forecast page", e);
        }
        for (var instance : YamcsServer.getInstances()) {
            addParameters(instance.getName());
        }
    }

    /**
     * Yamcs loads plugins after its instances, so each mission database is already built: the
     * parameters are attached to it here, unless its mdb section loads ConstellationMdb itself.
     * ponytail: an instance restarted alone (not Yamcs) reloads its database without them; the
     * ConstellationMdb line in its mdb section keeps them through that.
     */
    static void addParameters(String instance) {
        var mdb = MdbFactory.getInstance(instance);
        if (mdb == null || mdb.getSpaceSystem("/" + SPACE) != null) {
            return;
        }
        var own = MdbFactory.createInstance(
                List.of(YConfiguration.wrap(Map.of("type", ConstellationMdb.class.getName()))), false, false);
        var loaded = own.getSpaceSystem("/" + SPACE);
        var system = new SpaceSystem(SPACE);
        system.setQualifiedName("/" + SPACE);
        mdb.addSpaceSystem(system);
        loaded.getParameterTypes().forEach(system::addParameterType);
        loaded.getParameters().forEach(system::addParameter);
        mdb.buildIndexMaps();
    }
}
